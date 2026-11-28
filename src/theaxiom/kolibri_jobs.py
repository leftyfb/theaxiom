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
import stat
import shutil
import subprocess
import tempfile
import threading
import time

from . import acquisition_queue, catalog_store, kolibri_native as native, kolibri_scope as scope, kolibri_publication


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
        self.reservations = {}

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
        base = self.packages
        for path in (base, base/sid):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Native package parent is not an owned directory")
        return base / sid / token

    def _selection(self, artifact):
        fields = dict(artifact["selection"])
        fields["nodes"] = tuple(fields["nodes"])
        return native.Selection(**fields)

    def verify(self, sid, artifact):
        self.guard(0)
        return native.verify(self._package(sid, artifact), self._selection(artifact), artifact["manifest_sha256"])

    @contextmanager
    def _operation(self, sid, *, reserved=False):
        with self.lock:
            if not reserved:
                if sid in self.active:
                    raise ValueError("Educational source is busy")
                self.active.add(sid)
            elif sid not in self.active:
                raise ValueError("Educational reservation missing")
        try:
            yield
        finally:
            with self.lock:
                self.active.remove(sid)
                self.reservations.pop(sid, None)

    def download(self, sid, selection, *, update=False, reserved=False, stop=lambda: False):
        """Run a bounded explicit selection; candidate never changes live content."""
        with self._operation(sid, reserved=reserved):
            job = self.read(sid)
            if update:
                old = self._selection(job["installed"])
                if (selection.channel_id != old.channel_id or selection.nodes != old.nodes
                        or selection.allow_full != old.allow_full or selection.version <= old.version
                        or selection.base_url != old.base_url):
                    raise ValueError("Updates must preserve the installed selection and advance its version")
                if job.get("candidate") or job.get("previous"):
                    raise ValueError("Resolve existing staged or rollback package first")
            elif job.get("installed"):
                raise ValueError("Educational content already installed")
            token = secrets.token_hex(16)
            artifact = {"selection": asdict(selection), "token": token}
            target = self._package(sid, artifact)
            with self.lock:
                required = selection.media_bytes + scope.MAX_DATABASE_BYTES
                self.guard(required + sum(self.reservations.values()))
                self.reservations[sid] = required
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

            def checked_guard(required):
                if stop():
                    raise acquisition_queue.Cancelled()
                self.guard(required)

            try:
                artifact["manifest_sha256"] = native.acquire(target, selection,
                    progress=progress, guard=checked_guard)
                self.verify(sid, artifact)
                job.pop("reviewed", None)
                self._write(sid, dict(job, state="completed", active=False,
                    downloaded_bytes=selection.media_bytes,
                    **{"candidate" if update else "installed": artifact}))
            except BaseException as exc:
                # Incomplete files never become a verified artifact.
                if target.exists() and not target.is_symlink():
                    self.guard(0)
                    shutil.rmtree(target)
                self._write(sid, dict(job, state="cancelled" if isinstance(exc, acquisition_queue.Cancelled) else "failed", active=False, detail=str(exc)[:300]))
                raise

    def save_reviewed(self, sid, selection):
        with self._operation(sid):
            job = self.read(sid)
            if job.get("installed"):
                raise ValueError("Educational content already installed")
            self._write(sid, dict(job, reviewed=asdict(selection)))

    def reviewed(self, sid):
        job = self.read(sid)
        if job.get("installed") or not job.get("reviewed"):
            raise ValueError("Review the educational topic selection first")
        return self._selection({"selection": job["reviewed"]})

    def _manage(self, *args):
        env = dict(os.environ, KOLIBRI_HOME=str(self.home))
        self.runner([str(self.executable), "manage", *args], env=env, check=True, timeout=3600)

    def _import(self, sid, artifact):
        self.verify(sid, artifact)
        with self.lock:
            self.guard(self._selection(artifact).media_bytes + scope.MAX_DATABASE_BYTES + sum(self.reservations.values()))
        env = dict(os.environ, KOLIBRI_HOME=str(self.home))
        selection = self._selection(artifact)
        for command in native.import_commands(self.executable, self._package(sid, artifact), selection):
            self.runner(command, env=env, check=True, timeout=3600)
        database = native._database(self._package(sid, artifact), selection.channel_id)
        kolibri_publication.verify_live_scope(self.registry, database, selection)

    def _identity(self, artifact):
        selection = self._selection(artifact)
        return {"channel_id": selection.channel_id, "version": selection.version,
                "manifest_sha256": artifact["manifest_sha256"]}

    def _acknowledge(self, callback, published):
        # A shared content checksum can influence native availability in
        # another channel. Recheck every live reviewed scope before reopening.
        for sid, identity in published.items():
            job = self.read(sid)
            artifact = next((job.get(key) for key in ("installed", "candidate", "previous")
                             if job.get(key) and self._identity(job[key]) == identity), None)
            if artifact is None:
                raise ValueError("Published native package is not in the trusted ledger")
            selected = self._selection(artifact)
            database = native._database(self._package(sid, artifact), selected.channel_id)
            kolibri_publication.verify_live_scope(self.registry, database, selected)
        callback(published)

    def publish(self, sid, enabled):
        with self._operation(sid):
            job = self.read(sid)
            artifact = job.get("installed")
            if not artifact:
                raise ValueError("Verified educational content required")
            if enabled:
                self.verify(sid, artifact)
            with self.registry.transaction() as acknowledge:
                published = self.registry.read()
                if enabled:
                    if sid in published:
                        self._acknowledge(acknowledge, published)
                        return
                    identity = self._identity(artifact)
                    if any(r["channel_id"] == identity["channel_id"] for r in published.values()):
                        raise ValueError("Channel already published under another source")
                    self._import(sid, artifact)
                    published[sid] = identity
                elif sid in published:
                    self._manage("deletechannel", published.pop(sid)["channel_id"])
                self._acknowledge(acknowledge, published)

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
                self._acknowledge(acknowledge, published)

    def delete(self, sid, *, staged=False):
        with self._operation(sid):
            job = self.read(sid)
            if self.registry.pending.exists():
                raise ValueError("Resolve pending publication before deleting files")
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


    def recover(self):
        """Reconstruct prior acknowledged publication from verified private packages.

        This is an explicit operator action, not an implicit retry. Learner
        databases are retained. Any failure keeps both public routes closed.
        """
        with self.lock:
            if self.active:
                raise ValueError("Wait for educational jobs before recovery")
            with self.registry.transaction(recover=True) as acknowledge:
                desired = self.registry.read()
                restored = {}
                for sid, identity in desired.items():
                    job = self.read(sid)
                    match = next((key for key in ("installed", "candidate", "previous")
                                  if job.get(key) and self._identity(job[key]) == identity), None)
                    if match is None:
                        raise ValueError("Prior acknowledged package is missing")
                    artifact = job[match]
                    self.verify(sid, artifact)
                    restored[sid] = (job, match, artifact)
                with self.registry._database() as conn:
                    actual = [scope._id(row[0]) for row in conn.execute(
                        "SELECT id FROM content_channelmetadata LIMIT 101")]
                for cid in actual:
                    self._manage("deletechannel", cid)
                for sid, (job, match, artifact) in restored.items():
                    self._import(sid, artifact)
                    if match != "installed":
                        previous = job.pop(match)
                        job[match] = job["installed"]
                        job["installed"] = previous
                        self._write(sid, job)
                self._acknowledge(acknowledge, desired)
