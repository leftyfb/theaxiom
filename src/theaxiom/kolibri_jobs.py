"""Private job ledger and lifecycle coordinator for verified native packages.

Native artifacts are kept outside the public runtime until explicitly
published. Every native mutation runs under the publication registry's
fail-closed transaction. The caller supplies validated storage and runtime
paths; no production installation or service startup is performed here.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

from . import catalog_store, kolibri_native as native, kolibri_scope as scope


class Jobs:
    def __init__(self, state, packages, registry, executable, home, guard=lambda n: None,
                 runner=subprocess.run):
        self.state = Path(state)
        self.packages = Path(packages)
        self.registry = registry
        self.executable = Path(executable)
        self.home = Path(home)
        self.guard = guard
        self.runner = runner
        self.lock = threading.RLock()
        self.active = set()

    def _path(self, sid):
        catalog_store.CatalogStore._validate_id(sid)
        return self.state / (sid + ".json")

    def read(self, sid):
        with self.lock:
            try:
                data = json.loads(self._path(sid).read_text())
            except FileNotFoundError:
                return {}
            if data.get("source_id") != sid:
                raise ValueError("Invalid educational job identity")
            if data.get("state") == "downloading" and sid not in self.active:
                data = dict(data, state="interrupted", active=False)
            return data

    def _write(self, sid, data):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock:
            fd, path = tempfile.mkstemp(dir=self.state, prefix=".kolibri-")
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(dict(data, source_id=sid), stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(path, self._path(sid))
                fd = os.open(self.state, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                Path(path).unlink(missing_ok=True)

    def _package(self, sid, artifact):
        catalog_store.CatalogStore._validate_id(sid)
        token = artifact["token"]
        scope._id(token)
        return self.packages / sid / token

    def _selection(self, artifact):
        fields = dict(artifact["selection"])
        fields["nodes"] = tuple(fields["nodes"])
        return native.Selection(**fields)

    def verify(self, sid, artifact):
        self.guard(0)
        return native.verify(self._package(sid, artifact), self._selection(artifact), artifact["manifest_sha256"])

    @contextmanager
    def _operation(self, sid):
        with self.lock:
            if sid in self.active:
                raise ValueError("Educational source is busy")
            self.active.add(sid)
        try:
            yield
        finally:
            with self.lock:
                self.active.remove(sid)

    def download(self, sid, selection, *, update=False):
        """Run a bounded explicit selection; candidate never changes live content."""
        with self._operation(sid):
            job = self.read(sid)
            if update:
                old = self._selection(job["installed"])
                if (selection.channel_id != old.channel_id or selection.nodes != old.nodes
                        or selection.allow_full != old.allow_full or selection.version <= old.version):
                    raise ValueError("Updates must preserve the installed selection and advance its version")
                if job.get("candidate") or job.get("previous"):
                    raise ValueError("Resolve existing staged or rollback package first")
            elif job.get("installed"):
                raise ValueError("Educational content already installed")
            token = secrets.token_hex(16)
            artifact = {"selection": asdict(selection), "token": token}
            target = self._package(sid, artifact)
            self.guard(selection.media_bytes + scope.MAX_DATABASE_BYTES)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            job = dict(job, state="downloading", active=True,
                       downloaded_bytes=0, expected_bytes=selection.media_bytes, detail="")
            self._write(sid, job)
            started = time.monotonic()
            last = 0

            def progress(done, total):
                nonlocal last
                now = time.monotonic()
                if now-last < .5 and done != total:
                    return
                last = now
                self._write(sid, dict(job, downloaded_bytes=done, expected_bytes=total,
                    sampled_at=time.time(), speed_bytes_per_second=done/max(now-started, .1)))

            try:
                artifact["manifest_sha256"] = native.acquire(target, selection,
                    progress=progress, guard=self.guard)
                self.verify(sid, artifact)
                self._write(sid, dict(job, state="completed", active=False,
                    downloaded_bytes=selection.media_bytes,
                    **{"candidate" if update else "installed": artifact}))
            except BaseException as exc:
                # Incomplete files never become a verified artifact.
                if target.exists() and not target.is_symlink():
                    self.guard(0)
                    shutil.rmtree(target)
                self._write(sid, dict(job, state="failed", active=False, detail=str(exc)[:300]))
                raise

    def _manage(self, *args):
        env = dict(os.environ, KOLIBRI_HOME=str(self.home))
        self.runner([str(self.executable), "manage", *args], env=env, check=True, timeout=3600)

    def _import(self, sid, artifact):
        self.verify(sid, artifact)
        env = dict(os.environ, KOLIBRI_HOME=str(self.home))
        selection = self._selection(artifact)
        for command in native.import_commands(self.executable, self._package(sid, artifact), selection):
            self.runner(command, env=env, check=True, timeout=3600)

    def _identity(self, artifact):
        selection = self._selection(artifact)
        return {"channel_id": selection.channel_id, "version": selection.version,
                "manifest_sha256": artifact["manifest_sha256"]}

    def publish(self, sid, enabled):
        with self._operation(sid):
            job = self.read(sid)
            artifact = job.get("installed")
            if not artifact:
                raise ValueError("Verified educational content required")
            self.verify(sid, artifact)
            with self.registry.transaction() as acknowledge:
                published = self.registry.read()
                if enabled:
                    if sid in published:
                        acknowledge(published)
                        return
                    identity = self._identity(artifact)
                    if any(r["channel_id"] == identity["channel_id"] for r in published.values()):
                        raise ValueError("Channel already published under another source")
                    self._import(sid, artifact)
                    published[sid] = identity
                elif sid in published:
                    self._manage("deletechannel", published.pop(sid)["channel_id"])
                acknowledge(published)

    def switch(self, sid, *, rollback=False):
        """Promote or roll back the selected artifact without replacing learner DBs."""
        with self._operation(sid):
            job = self.read(sid)
            key = "previous" if rollback else "candidate"
            artifact = job.get(key)
            old = job.get("installed")
            if not artifact or not old:
                raise ValueError("No verified educational replacement")
            self.verify(sid, artifact)
            self.verify(sid, old)
            with self.registry.transaction() as acknowledge:
                published = self.registry.read()
                if sid in published:
                    self._manage("deletechannel", published[sid]["channel_id"])
                    self._import(sid, artifact)
                    published[sid] = self._identity(artifact)
                job.pop(key)
                job["installed"] = artifact
                job["candidate" if rollback else "previous"] = old
                self._write(sid, job)
                acknowledge(published)

    def delete(self, sid, *, staged=False):
        with self._operation(sid):
            job = self.read(sid)
            if not staged and sid in self.registry.read():
                raise ValueError("Unpublish educational content before deleting")
            self.guard(0)
            fields = ("candidate", "previous") if staged else ("installed", "candidate", "previous")
            for field in fields:
                artifact = job.get(field)
                if artifact:
                    self.verify(sid, artifact)
                    shutil.rmtree(self._package(sid, artifact))
                    job.pop(field)
            self._write(sid, job if staged else {})
