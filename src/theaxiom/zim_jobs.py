"""Durable, bounded ZIM acquisition records independent of browser requests.

A single application worker serializes writes. A file lock prevents duplicate
workers across processes. Backend termination interrupts a transfer; persisted
state is reported as interrupted, never silently retried. A separate systemd
worker and resumable staging are future lifecycle work.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from . import catalog_store, storage_settings, zim_source, pdf_source

ACTIVE = {"queued", "downloading"}
LOCK = threading.RLock()


def directory():
    return storage_settings.SETTINGS_PATH.with_name("zim-jobs")


def path_for(sid):
    catalog_store.CatalogStore._validate_id(sid)
    return directory() / (sid + ".json")


def read(sid):
    target = path_for(sid)
    try:
        with target.open("rb") as stream:
            raw = stream.read(16385)
    except FileNotFoundError:
        return None
    if len(raw) > 16384:
        raise ValueError("ZIM job record exceeds size limit")
    job = json.loads(raw)
    if (not isinstance(job, dict) or job.get("source_id") != sid
            or job.get("state") not in ACTIVE | {"completed", "failed", "interrupted"}
            or not isinstance(job.get("configuration"), dict)):
        raise ValueError("Invalid ZIM job record")
    return job


def save(job):
    target = path_for(job["source_id"])
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".job-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(job, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def worker_lock():
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def status(sid):
    with LOCK:
        job = read(sid)
        if job and job["state"] in ACTIVE:
            try:
                with worker_lock():
                    job.update(state="interrupted", active=False, detail="Transfer interrupted; retry explicitly.")
                    save(job)
            except BlockingIOError:
                pass
        return job


def identity(config):
    url, size, digest = zim_source.candidate(config)
    return {"format": "zim", "source_url": url, "expected_size_bytes": str(size) if size else "",
            "sha256": digest or ""}


def matches(job, config):
    try:
        return job is not None and job["configuration"] == identity(config)
    except ValueError:
        return False


def installed(job, config):
    if not matches(job, config) or job["state"] != "completed":
        return None
    try:
        artifact = job["artifact"]
        expected_name = job["source_id"] + "." + artifact["sha256"] + ".zim"
        if artifact["filename"] != expected_name or len(artifact["sha256"]) != 64:
            return None
        with pdf_source._directory("library", format_name="zim") as (fd, mount, _):
            with pdf_source._file(fd, mount, expected_name, artifact["bytes"]):
                return artifact
    except (OSError, ValueError, KeyError, TypeError):
        return None


def start(sid, config):
    configuration = identity(config)
    with LOCK:
        # Hold the OS lock continuously from job creation until worker exit.
        guard = worker_lock()
        guard.__enter__()
        try:
            old = read(sid)
            if old and old["state"] == "completed" and installed(old, config):
                raise ValueError("Archive already installed; replacement is not yet supported.")
            # Preflight both paths before returning acceptance; no network yet.
            with (pdf_source._directory("staging", writing=True, format_name="zim") as (sfd, sm, _),
                  pdf_source._directory("library", writing=True, format_name="zim") as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve + (int(configuration["expected_size_bytes"] or 96)):
                    raise ValueError("Insufficient writable ZIM storage capacity")
            job = dict(source_id=sid, configuration=configuration, state="queued", active=True,
                       verified=False, downloaded_bytes=0,
                       expected_bytes=int(configuration["expected_size_bytes"]) if configuration["expected_size_bytes"] else None,
                       sampled_at=time.time())
            save(job)
            thread = threading.Thread(target=_run, args=(job, guard), daemon=True, name="zim-acquisition")
            thread.start()
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return {"accepted": True, "source_id": sid, "state": "queued"}


def _run(job, guard):
    last = 0.0
    start_time = time.monotonic()
    def progress(current, total):
        nonlocal last
        now = time.monotonic()
        if now - last >= 1:
            job.update(downloaded_bytes=current, expected_bytes=total, sampled_at=time.time(),
                       speed_bytes_per_second=current / max(1, now-start_time))
            save(job)
            last = now
    try:
        job.update(state="downloading", sampled_at=time.time())
        save(job)
        artifact = zim_source.download(job["source_id"], job["configuration"], progress=progress)
        job.update(state="completed", active=False, verified=True, artifact=artifact,
                   downloaded_bytes=artifact["bytes"], expected_bytes=artifact["bytes"], sampled_at=time.time())
    except Exception as exc:
        # Network/OS error strings can disclose local paths; use a bounded generic message.
        detail = str(exc)[:240] if isinstance(exc, ValueError) else "ZIM acquisition failed (network or storage I/O)."
        job.update(state="failed", active=False, verified=False, detail=detail, sampled_at=time.time())
    finally:
        try:
            save(job)
        finally:
            guard.__exit__(None, None, None)
