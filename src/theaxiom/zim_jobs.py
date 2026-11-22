"""Durable ZIM jobs with serial acquisition, explicit controls, and restart recovery."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from . import catalog_store, storage_settings, zim_source, pdf_source

HISTORY_LIMIT = 12
MAX_RETRIES = 3

ACTIVE = {"queued", "downloading", "verifying", "pausing", "cancelling", "publishing"}
CONTROLS = {}
LOCK = threading.RLock()


def directory():
    return storage_settings.SETTINGS_PATH.with_name("zim-jobs")


def path_for(sid):
    catalog_store.CatalogStore._validate_id(sid)
    return directory() / (sid + ".json")



def candidate_path_for(sid):
    """Replacement metadata is separate from the installed serving record."""
    catalog_store.CatalogStore._validate_id(sid)
    return directory() / (sid + ".candidate.json")


def read_candidate(sid):
    """Read candidate metadata without modifying the installed archive."""
    target = candidate_path_for(sid)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        import stat
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe ZIM candidate record")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(16385)
    finally:
        os.close(fd)
    if len(raw) > 16384:
        raise ValueError("ZIM candidate record exceeds size limit")
    try:
        candidate = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid ZIM candidate record") from exc
    if (not isinstance(candidate, dict) or candidate.get("source_id") != sid
            or candidate.get("state") not in {"prepared", "verified"}
            or not isinstance(candidate.get("configuration"), dict)
            or not isinstance(candidate.get("base_artifact"), dict)):
        raise ValueError("Invalid ZIM candidate record")
    try:
        if identity(candidate["configuration"]) != candidate["configuration"]:
            raise ValueError("Invalid ZIM candidate configuration")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid ZIM candidate configuration") from exc
    if candidate["state"] == "verified" and not isinstance(candidate.get("artifact"), dict):
        raise ValueError("Invalid verified ZIM candidate")
    return candidate


def prepare_candidate(sid, config):
    """Persist replacement intent only; do not download or activate it."""
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            live = read(sid)
            if (not live or live.get("state") != "completed"
                    or live.get("verified") is not True
                    or not installed(live, live["configuration"])):
                raise ValueError("A verified installed archive is required for replacement")
            if configuration == live["configuration"]:
                raise ValueError("Replacement candidate must differ from installed archive")
            target = candidate_path_for(sid)
            if target.exists() or target.is_symlink():
                raise ValueError("Replacement candidate already exists")
            candidate = {"source_id": sid, "state": "prepared",
                         "configuration": configuration, "base_artifact": live["artifact"],
                         "created_at": time.time()}
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".candidate-", dir=target.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(candidate, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(name, target, follow_symlinks=False)
                dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                Path(name).unlink(missing_ok=True)
            return candidate




def acquire_candidate(sid, config):
    """Synchronously download and verify replacement bytes without switching the live archive.

    Uses a separate staging identity; callers must explicitly activate later.
    On failure, candidate metadata and the installed job remain intact.
    """
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            live = read(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate["base_artifact"]
                    or not installed(live, live["configuration"])):
                raise ValueError("Installed archive changed; replacement requires review")
            replacement_id = sid + "-candidate"
            catalog_store.CatalogStore._validate_id(replacement_id)
            if staged_size(replacement_id):
                raise ValueError("Replacement staging already exists; manual recovery required")
            artifact = zim_source.download_resumable(
                replacement_id, configuration, offset=0, promote_lock=LOCK)
            candidate["state"] = "verified"
            candidate["artifact"] = artifact
            target = candidate_path_for(sid)
            fd, name = tempfile.mkstemp(prefix=".candidate-", dir=target.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(candidate, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, target)
                dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                Path(name).unlink(missing_ok=True)
            return candidate


def discard_candidate(sid, config):
    """Explicitly discard preparation metadata, never archive bytes or the live job.

    Candidate acquisition is not enabled yet; refuse to discard any record
    outside the prepared state rather than accidentally erasing recovery data.
    """
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] != "prepared":
                raise ValueError("Verified replacement cannot be discarded without artifact cleanup")
            live = read(sid)
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate.get("base_artifact")):
                raise ValueError("Installed archive changed; candidate requires review")
            target = candidate_path_for(sid)
            if target.is_symlink():
                raise ValueError("Unsafe replacement candidate record")
            target.unlink()
            dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return {"accepted": True, "source_id": sid, "state": "discarded"}


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
            or job.get("state") not in ACTIVE | {"completed", "failed", "interrupted", "paused", "cancelled"}
            or not isinstance(job.get("configuration"), dict)):
        raise ValueError("Invalid ZIM job record")
    return job


def _record_attempt(job, *, started=False):
    """Keep bounded, durable acquisition outcomes without copying archive bytes."""
    history = job.setdefault("attempt_history", [])
    if started:
        history.append({"started_at": time.time(), "resume_offset": job.get("downloaded_bytes", 0),
                        "source_url": job["configuration"]["source_url"]})
        del history[:-HISTORY_LIMIT]
    elif history:
        entry = history[-1]
        entry.update(finished_at=time.time(), state=job["state"],
                     downloaded_bytes=job.get("downloaded_bytes", 0),
                     expected_bytes=job.get("expected_bytes"),
                     etag=job.get("etag"), last_modified=job.get("last_modified"),
                     detail=job.get("detail", "")[:240],
                     retained=job.get("retained", False))


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
                    try:
                        job["downloaded_bytes"] = staged_size(sid)
                    except (OSError, ValueError):
                        job["downloaded_bytes"] = 0
                    job.update(state="paused", active=False, detail="Backend restarted; resume explicitly.")
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



def staged_size(sid):
    import stat
    with pdf_source._directory("staging", writing=True, format_name="zim") as (root, _, _):
        try:
            fd = os.open(zim_source.partial_name(sid), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            return 0
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Unsafe staged ZIM file")
            return info.st_size
        finally:
            os.close(fd)


def staged_present(sid):
    """Detect even zero-byte or unsafe leftovers before a fresh download."""
    with pdf_source._directory("staging", writing=True, format_name="zim") as (root, _, _):
        try:
            os.stat(zim_source.partial_name(sid), dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True


def start(sid, config, *, resume=False, restart=False, retry=False):
    if retry and (resume or restart):
        raise ValueError("Retry cannot be combined with resume or restart")
    if restart and resume:
        raise ValueError("Restart and resume are mutually exclusive")
    configuration = identity(config)
    with LOCK:
        guard = worker_lock()
        guard.__enter__()
        try:
            old = read(sid)
            # A changed source URL/checksum must not bypass the installed-content
            # guard: overwriting the completed job would orphan the published
            # archive before a replacement had been verified and activated.
            if old and old.get("state") == "completed":
                raise ValueError("Archive already installed; replacement is not yet supported.")
            if retry:
                if not old or old["state"] != "failed" or not matches(old, config):
                    raise ValueError("Retry requires a matching failed ZIM job")
                if old.get("retry_count", sum(entry.get("operation") == "retry" for entry in old.get("attempt_history", []))) >= MAX_RETRIES:
                    raise ValueError("ZIM retry limit reached; explicit restart required")
            previous_retries = (old.get("retry_count", sum(
                entry.get("operation") == "retry"
                for entry in old.get("attempt_history", []))) if old else 0)
            if restart and (not old or old["state"] not in {"paused", "failed", "interrupted", "cancelled"}):
                raise ValueError("Restart requires an existing stopped ZIM job")
            if resume or (retry and staged_size(sid) > 0):
                if not old or old["state"] not in {"paused", "failed", "interrupted"} or not matches(old, config):
                    raise ValueError("No matching paused ZIM download")
                offset = staged_size(sid)
                validator = old.get("etag") or old.get("last_modified")
                if not offset or not validator:
                    raise ValueError("Safe resume requires retained bytes and a publisher validator")
                if old.get("expected_bytes") and offset >= old["expected_bytes"]:
                    raise ValueError("Invalid resumable offset")
                job = old
            else:
                # A stale partial is never safe to truncate, even if its job
                # record says cancelled/completed (for example after a crash
                # between the record and filesystem operations).
                if staged_present(sid):
                    raise ValueError("Partial download exists; resume or cancel it first")
                if old and old["state"] in {"paused", "failed", "interrupted"} and not (restart or retry):
                    raise ValueError("Restart requires explicit confirmation")
                offset, validator = 0, None
                job = dict(source_id=sid, configuration=configuration, verified=False, downloaded_bytes=0,
                           expected_bytes=int(configuration["expected_size_bytes"]) if configuration["expected_size_bytes"] else None)
                if retry:
                    # Preserve the retry budget across fresh retries with no
                    # staged bytes; replacing the record must not reset it.
                    job["attempt_history"] = list(old.get("attempt_history", []))
            with (pdf_source._directory("staging", writing=True, format_name="zim") as (sfd, sm, _),
                  pdf_source._directory("library", writing=True, format_name="zim") as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                need = max(96, (int(configuration["expected_size_bytes"] or 0) - offset))
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve + need:
                    raise ValueError("Insufficient writable ZIM storage capacity")
            job.update(state="queued", active=True, detail="", downloaded_bytes=offset, sampled_at=time.time())
            _record_attempt(job, started=True)
            if retry:
                job["attempt_history"][-1]["operation"] = "retry"
                # Compute from the previous persisted record before mutating
                # the current attempt. On a retained partial, job is old.
                job["retry_count"] = previous_retries + 1
            save(job)
            signal = threading.Event()
            CONTROLS[sid] = (signal, None)
            threading.Thread(target=_run, args=(job, guard, signal, offset, validator),
                             daemon=True, name="zim-acquisition").start()
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return {"accepted": True, "source_id": sid, "state": "queued"}



def revalidate(sid, config):
    """Explicitly recheck a complete retained ZIM with no network request."""
    configuration = identity(config)
    with LOCK:
        guard = worker_lock()
        guard.__enter__()
        try:
            old = read(sid)
            if not old or old["state"] not in {"failed", "paused", "interrupted"} or not matches(old, config):
                raise ValueError("No matching retained ZIM download")
            if installed(old, config):
                raise ValueError("Archive already installed")
            expected = old.get("expected_bytes")
            actual = staged_size(sid)
            if type(expected) is not int or expected < 96 or actual != expected:
                raise ValueError("Local revalidation requires a complete retained file of known size")
            with (pdf_source._directory("staging", writing=True, format_name="zim") as (sfd, sm, _),
                  pdf_source._directory("library", writing=True, format_name="zim") as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve:
                    raise ValueError("Insufficient writable ZIM storage capacity")
            old.update(state="queued", active=True, verified=False,
                       detail="Local revalidation requested; no download.", sampled_at=time.time())
            _record_attempt(old, started=True)
            old["attempt_history"][-1]["operation"] = "revalidate"
            save(old)
            signal = threading.Event()
            CONTROLS[sid] = (signal, None)
            threading.Thread(target=_run, args=(old, guard, signal, expected, None),
                             kwargs={"local": True}, daemon=True,
                             name="zim-local-revalidation").start()
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return {"accepted": True, "source_id": sid, "state": "queued"}


def _retain_cancelled(job, sid):
    job.update(state="paused", active=False, verified=False,
               detail="Download cancelled; partial retained for explicit resume or discard.",
               retained=staged_size(sid) > 0, sampled_at=time.time())
    save(job)
    return {"accepted": True, "state": "paused"}


def control(sid, config, action):
    if action not in {"pause", "cancel", "cancel-keep"}:
        raise ValueError("Invalid ZIM control")
    with LOCK:
        job = status(sid)
        if not matches(job, config):
            raise ValueError("No matching ZIM job")
        if job["state"] == "publishing":
            raise ValueError("Verified ZIM publication is already finalizing")
        if job["state"] in ACTIVE:
            pair = CONTROLS.get(sid)
            if not pair:
                raise ValueError("Transfer is owned by another process")
            signal, previous = pair
            if previous:
                if previous != action:
                    raise ValueError("Stop request already pending")
                # Repeated HTTP requests must not reset an accepted stop.
                return {"accepted": True, "state": job["state"]}
            CONTROLS[sid] = (signal, action)
            job.update(state="pausing" if action == "pause" else "cancelling",
                       detail="Stopping transfer…", sampled_at=time.time())
            save(job)
            signal.set()
            return {"accepted": True, "state": job["state"]}
        if action == "cancel-keep" and job["state"] in {"paused", "failed", "interrupted"}:
            with worker_lock():
                return _retain_cancelled(job, sid)
        if action == "cancel" and job["state"] in {"paused", "failed", "interrupted"}:
            with worker_lock():
                if staged_present(sid):
                    zim_source.discard_partial(sid)
                job.update(state="cancelled", active=False, verified=False, downloaded_bytes=0,
                           detail="Download cancelled; partial removed.", sampled_at=time.time())
                save(job)
            return {"accepted": True, "state": "cancelled"}
        if action == "pause" and job["state"] == "paused":
            return {"accepted": True, "state": "paused"}
        raise ValueError("ZIM job cannot be " + action + "d in current state")


def _run(job, guard, signal, offset, validator, *, local=False):
    sid = job["source_id"]
    last = 0.0
    begun = time.monotonic()

    def progress(current, total, metadata):
        nonlocal last
        now = time.monotonic()
        # The worker can report progress after an operator has requested a
        # stop. Serialize both writes so a late verification notification
        # cannot replace the durable pausing/cancelling state in the UI.
        with LOCK:
            for key in ("etag", "last_modified"):
                if metadata.get(key):
                    job[key] = metadata[key]
            if total is not None:
                job["expected_bytes"] = total
            if metadata.get("verifying") and job["state"] not in {"pausing", "cancelling"}:
                job["state"] = "verifying"
            if now - last >= 1 or metadata.get("verifying") or current == offset:
                job.update(downloaded_bytes=current, sampled_at=time.time(),
                           speed_bytes_per_second=max(0, current-offset) / max(1, now-begun))
                save(job)
                last = now

    try:
        # A stop can be accepted while the newly spawned worker is still
        # queued. Never replace that durable request with "downloading".
        with LOCK:
            if job["state"] not in {"pausing", "cancelling"}:
                job.update(state="verifying" if local else "downloading", sampled_at=time.time())
                save(job)
        def promoting():
            job.update(state="publishing", sampled_at=time.time())
            save(job)

        if local:
            artifact = zim_source.revalidate_retained(
                sid, job["configuration"], expected_bytes=offset,
                progress=progress, stopped=signal.is_set,
                promote_lock=LOCK, promoting=promoting)
        else:
            artifact = zim_source.download_resumable(
                sid, job["configuration"], offset=offset,
                validator=validator, progress=progress, stopped=signal.is_set,
                promote_lock=LOCK, promoting=promoting)
        job.update(state="completed", active=False, verified=True, artifact=artifact,
                   downloaded_bytes=artifact["bytes"], expected_bytes=artifact["bytes"], sampled_at=time.time())
    except zim_source.TransferStopped:
        job.update(state="paused", active=False, verified=False,
                   detail="Download paused; partial retained.", sampled_at=time.time())
    except Exception as exc:
        detail = str(exc)[:240] if isinstance(exc, ValueError) else "ZIM acquisition failed (network or storage I/O)."
        job.update(state="failed", active=False, verified=False, detail=detail, sampled_at=time.time())
    finally:
        # Serialize the final outcome with control() so a cancellation accepted
        # just as the network fails cannot be overwritten by a stale failure.
        with LOCK:
            try:
                request = CONTROLS.get(sid, (None, None))[1]
                if request == "cancel-keep" and job["state"] != "completed":
                    job.update(state="paused", active=False, verified=False,
                               detail="Download cancelled; partial retained for explicit resume or discard.",
                               retained=staged_size(sid) > 0, sampled_at=time.time())
                if request == "cancel" and job["state"] != "completed":
                    try:
                        zim_source.discard_partial(sid)
                        job.update(state="cancelled", active=False, verified=False,
                                   downloaded_bytes=0, detail="Download cancelled; staged bytes removed.",
                                   sampled_at=time.time())
                    except FileNotFoundError:
                        job.update(state="cancelled", active=False, verified=False,
                                   downloaded_bytes=0, detail="Download cancelled; no staged bytes remain.",
                                   sampled_at=time.time())
                    except (OSError, ValueError):
                        job.update(state="failed", active=False, verified=False,
                                   detail="Cancellation could not clean the configured staging mount.",
                                   sampled_at=time.time())
                elif request == "pause" and job["state"] == "failed":
                    job.update(state="paused", active=False, verified=False,
                               detail="Download paused after transport failure.", sampled_at=time.time())
                if job["state"] in {"failed", "paused"}:
                    try:
                        job["downloaded_bytes"] = staged_size(sid)
                    except (OSError, ValueError):
                        pass
                job["retained"] = job["state"] in {"failed", "paused"} and job.get("downloaded_bytes", 0) > 0
                _record_attempt(job)
                save(job)
            finally:
                CONTROLS.pop(sid, None)
                guard.__exit__(None, None, None)
