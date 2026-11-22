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
CANDIDATE_CONTROLS = {}
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
            or candidate.get("state") not in {"prepared", "downloading", "verifying", "cancelling", "interrupted", "cancelled", "failed", "verified", "activated", "rolled_back"}
            or not isinstance(candidate.get("configuration"), dict)
            or not isinstance(candidate.get("base_artifact"), dict)):
        raise ValueError("Invalid ZIM candidate record")
    try:
        if identity(candidate["configuration"]) != candidate["configuration"]:
            raise ValueError("Invalid ZIM candidate configuration")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid ZIM candidate configuration") from exc
    if candidate["state"] in {"verified", "activated", "rolled_back"} and not isinstance(candidate.get("artifact"), dict):
        raise ValueError("Invalid verified ZIM candidate")
    if candidate["state"] in {"activated", "rolled_back"} and not isinstance(candidate.get("previous_job"), dict):
        raise ValueError("Invalid activated ZIM candidate")
    return candidate


def candidate_status(sid):
    """Return replacement progress and mark orphaned workers interrupted.

    The serving archive is never changed. A held worker lock means another
    acquisition may still be running, so do not mark it interrupted.
    """
    candidate = read_candidate(sid)
    if not candidate or candidate["state"] not in {"downloading", "verifying", "cancelling"}:
        return candidate
    with LOCK:
        try:
            with worker_lock():
                candidate = read_candidate(sid)
                if candidate and candidate["state"] in {"downloading", "verifying", "cancelling"}:
                    candidate["state"] = "interrupted"
                    candidate["error"] = "Replacement worker stopped; staged bytes retained for review"
                    _save_candidate(candidate)
        except BlockingIOError:
            return candidate
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
    """Download and verify replacement bytes without switching the live archive.

    Uses a separate staging identity; callers must explicitly activate later.
    On failure, candidate metadata and the installed job remain intact.
    """
    configuration = identity(config)
    with worker_lock():
        candidate = read_candidate(sid)
        live = read(sid)
        if not candidate or candidate["configuration"] != configuration:
            raise ValueError("No matching replacement candidate")
        if candidate["state"] != "prepared":
            raise ValueError("Replacement candidate already verified; activation required")
        if (not live or live.get("state") != "completed"
                or live.get("artifact") != candidate["base_artifact"]
                or not installed(live, live["configuration"])):
            raise ValueError("Installed archive changed; replacement requires review")
        replacement_id = sid + "-candidate"
        catalog_store.CatalogStore._validate_id(replacement_id)
        # Reject zero-length and unsafe leftovers too; the downloader
        # otherwise opens the existing source-scoped partial for writing.
        if staged_present(replacement_id):
            raise ValueError("Replacement staging already exists; manual recovery required")
        artifact = zim_source.download_resumable(
            sid, configuration, offset=0, promote_lock=LOCK,
            staging_id=replacement_id)
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


def start_candidate_acquisition(sid, config):
    """Queue a replacement download without blocking the HTTP request."""
    configuration = identity(config)
    with LOCK:
        guard = worker_lock()
        guard.__enter__()
        try:
            candidate = read_candidate(sid)
            live = read(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] not in {"prepared", "failed", "interrupted", "cancelled"}:
                raise ValueError("Replacement candidate is not ready for acquisition")
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate["base_artifact"]
                    or not installed(live, live["configuration"])):
                raise ValueError("Installed archive changed; replacement requires review")
            staging_id = sid + "-candidate"
            catalog_store.CatalogStore._validate_id(staging_id)
            offset = staged_size(staging_id)
            if offset and not (candidate.get("etag") or candidate.get("last_modified")):
                raise ValueError("Cannot safely resume without publisher validator; cancel and discard staging first")
            validator = (candidate.get("etag") or candidate.get("last_modified")) if offset else None
            if not offset:
                # A fresh transfer must not inherit validators from an earlier
                # failed attempt; those headers describe a different response.
                candidate.pop("etag", None)
                candidate.pop("last_modified", None)
            candidate["state"] = "downloading"
            candidate["downloaded_bytes"] = offset
            signal = threading.Event()
            CANDIDATE_CONTROLS[sid] = signal
            _save_candidate(candidate)
            try:
                threading.Thread(target=_run_candidate, args=(sid, configuration, guard, signal, offset, validator),
                                 daemon=True, name="zim-replacement").start()
            except BaseException:
                candidate["state"] = "prepared"
                candidate.pop("downloaded_bytes", None)
                _save_candidate(candidate)
                CANDIDATE_CONTROLS.pop(sid, None)
                raise
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return {"accepted": True, "source_id": sid, "state": "downloading"}


def _run_candidate(sid, configuration, guard, signal, offset, validator):
    last = 0.0

    def progress(current, total, metadata):
        nonlocal last
        now = time.monotonic()
        with LOCK:
            candidate = read_candidate(sid)
            if not candidate or candidate["state"] not in {"downloading", "verifying", "cancelling"}:
                return
            validator_changed = (
                (metadata.get("etag") and metadata["etag"] != candidate.get("etag")) or
                (metadata.get("last_modified") and metadata["last_modified"] != candidate.get("last_modified"))
            )
            if now - last < 0.5 and not metadata.get("verifying") and not validator_changed:
                return
            candidate["downloaded_bytes"] = current
            if total is not None:
                candidate["expected_bytes"] = total
            if metadata.get("etag"):
                candidate["etag"] = metadata["etag"]
            if metadata.get("last_modified"):
                candidate["last_modified"] = metadata["last_modified"]
            if metadata.get("verifying") and candidate["state"] != "cancelling":
                # A verification callback must not erase a pending stop request.
                candidate["state"] = "verifying"
            _save_candidate(candidate)
        last = now

    try:
        artifact = zim_source.download_resumable(
            sid, configuration, offset=offset, validator=validator, promote_lock=LOCK,
            staging_id=sid + "-candidate", progress=progress, stopped=signal.is_set)
        with LOCK:
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("Replacement candidate changed during acquisition")
            if candidate["state"] == "cancelling":
                # Promotion may already have completed before cancellation arrived.
                # Preserve the verified artifact so it is not orphaned; activation
                # remains a separate, explicit operator decision.
                candidate.update(state="verified", artifact=artifact)
                candidate["error"] = "Cancellation arrived after verification; archive retained, activation requires confirmation"
                _save_candidate(candidate)
                return
            candidate.update(state="verified", artifact=artifact)
            candidate.pop("error", None)
            _save_candidate(candidate)
    except zim_source.TransferStopped:
        with LOCK:
            candidate = read_candidate(sid)
            if candidate and candidate["configuration"] == configuration:
                candidate["state"] = "cancelled"
                candidate["error"] = "Replacement download cancelled; staged bytes retained"
                _save_candidate(candidate)
    except Exception as exc:
        with LOCK:
            candidate = read_candidate(sid)
            if candidate and candidate["configuration"] == configuration:
                candidate["state"] = "failed"
                candidate["error"] = str(exc)[:512]
                _save_candidate(candidate)
    finally:
        with LOCK:
            CANDIDATE_CONTROLS.pop(sid, None)
        guard.__exit__(None, None, None)


def cancel_candidate_acquisition(sid, config):
    """Request a cooperative stop; retain staged bytes for safe recovery."""
    configuration = identity(config)
    with LOCK:
        candidate = read_candidate(sid)
        if not candidate or candidate["configuration"] != configuration:
            raise ValueError("No matching replacement candidate")
        if candidate["state"] not in {"downloading", "verifying", "cancelling"}:
            raise ValueError("Replacement download is not running")
        signal = CANDIDATE_CONTROLS.get(sid)
        if signal is None:
            raise ValueError("Replacement worker belongs to another process")
        candidate["state"] = "cancelling"
        _save_candidate(candidate)
        signal.set()
        return {"accepted": True, "source_id": sid, "state": "cancelling"}


def activate_candidate(sid, config):
    """Switch the serving record only after the replacement artifact is installed.

    The old content-addressed archive remains on disk. Candidate metadata is
    retained for a subsequent explicit rollback operation.
    """
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] != "verified":
                raise ValueError("Replacement must be verified before activation")
            live = read(sid)
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate["base_artifact"]
                    or not installed(live, live["configuration"])):
                raise ValueError("Installed archive changed; activation refused")
            replacement = dict(source_id=sid, state="completed", active=False,
                               verified=True, configuration=configuration,
                               artifact=candidate["artifact"])
            if not installed(replacement, configuration):
                raise ValueError("Replacement archive is missing or invalid")
            # Preserve the complete original serving record for rollback.
            candidate["previous_job"] = live
            candidate["state"] = "activated"
            _save_candidate(candidate)
            save(replacement)
            return replacement


def rollback_candidate(sid, config):
    """Restore the previous verified serving record without removing either archive."""
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] != "activated":
                raise ValueError("Replacement has not been activated")
            current = read(sid)
            previous = candidate["previous_job"]
            if (not current or current.get("state") != "completed"
                    or current.get("configuration") != configuration
                    or current.get("artifact") != candidate["artifact"]):
                raise ValueError("Serving archive changed; rollback refused")
            if (previous.get("source_id") != sid or
                    previous.get("artifact") != candidate["base_artifact"] or
                    not installed(previous, previous["configuration"])):
                raise ValueError("Previous archive missing or invalid; rollback refused")
            # Persist rollback intent before changing the serving record.
            # A crash between writes must never reactivate the replacement.
            candidate["state"] = "rolled_back"
            _save_candidate(candidate)
            save(previous)
            return previous


def recover_candidate_transition(sid):
    """Recover interrupted activation/rollback without guessing which archive to serve.

    Activation writes its durable intent before changing the live job. Rollback
    writes the live job first. Reconcile only when the observed job matches
    exactly one of those two expected records.
    """
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            if not candidate or candidate["state"] not in {"activated", "rolled_back"}:
                return False
            previous = candidate["previous_job"]
            replacement = dict(source_id=sid, state="completed", active=False,
                               verified=True, configuration=candidate["configuration"],
                               artifact=candidate["artifact"])
            current = read(sid)
            if candidate["state"] == "activated":
                if current == replacement:
                    return False
                if current != previous:
                    raise ValueError("Serving record diverged; recovery refused")
                if not installed(replacement, candidate["configuration"]):
                    raise ValueError("Replacement archive missing; recovery refused")
                save(replacement)
                return True
            if current == previous:
                return False
            if current != replacement:
                raise ValueError("Serving record diverged; recovery refused")
            if not installed(previous, previous["configuration"]):
                raise ValueError("Previous archive missing; recovery refused")
            save(previous)
            return True


def _save_candidate(candidate):
    target = candidate_path_for(candidate["source_id"])
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


def discard_candidate(sid, config):
    """Retire safe inactive candidate metadata without deleting archive bytes.

    An activated candidate is recovery-critical and cannot be discarded.
    A rolled-back candidate may be retired only after verifying the original
    serving record is restored, enabling a future replacement attempt.
    """
    configuration = identity(config)
    with LOCK:
        with worker_lock():
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] not in {"prepared", "rolled_back", "failed", "cancelled", "interrupted"}:
                raise ValueError("Active or verified replacement cannot be discarded without artifact cleanup")
            if candidate["state"] in {"failed", "cancelled", "interrupted"}:
                # Never silently abandon partial bytes. Once staging is absent,
                # failed attempts may be retired and a new candidate prepared.
                if staged_present(sid + "-candidate"):
                    raise ValueError("Replacement staging remains; remove or recover staged bytes before discarding")
                if candidate.get("artifact"):
                    raise ValueError("Replacement artifact remains; manual cleanup required")
            live = read(sid)
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate.get("base_artifact")):
                raise ValueError("Installed archive changed; candidate requires review")
            if candidate["state"] == "rolled_back" and (
                    live != candidate.get("previous_job")
                    or not installed(live, live["configuration"])):
                raise ValueError("Rollback serving record is not safely restored")
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



def delete_installed(sid):
    """Explicitly delete an unpublished installed ZIM, never a candidate or shared path.

    Refuse pending replacement state and any active acquisition. The archive is
    unlinked by directory descriptor only after its managed name, size, inode
    type and single-link ownership have been verified.
    """
    import stat
    from . import publication
    with publication.LOCK:
        with LOCK:
            with worker_lock():
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish the source before deleting installed content")
                if read_candidate(sid) is not None:
                    raise ValueError("Retire the replacement candidate before deleting installed content")
                job = read(sid)
                if not job or job.get("state") != "completed" or job.get("active") is not False:
                    raise ValueError("No inactive completed ZIM installation to delete")
                artifact = installed(job, job["configuration"])
                if not artifact:
                    raise ValueError("Installed ZIM is missing or invalid; deletion refused")
                filename = artifact["filename"]
                if filename != f"{sid}.{artifact['sha256']}.zim":
                    raise ValueError("Invalid managed ZIM filename")
                with pdf_source._directory("library", writing=True, format_name="zim") as (fd, _, _):
                    file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        info = os.fstat(file_fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_size != artifact["bytes"]):
                            raise ValueError("Installed archive is not an exclusive regular file")
                        # Do not follow a path substituted after validation.
                        current = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                        if (current.st_ino, current.st_dev) != (info.st_ino, info.st_dev):
                            raise ValueError("Installed archive changed; deletion refused")
                        os.unlink(filename, dir_fd=fd)
                        os.fsync(fd)
                    finally:
                        os.close(file_fd)
                target = path_for(sid)
                target.unlink()
                dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
                return {"accepted": True, "source_id": sid, "state": "deleted"}


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
    # Resolve an interrupted, durable replacement transition before exposing
    # the serving job to the catalog or admin API.
    candidate = read_candidate(sid)
    if candidate and candidate["state"] in {"activated", "rolled_back"}:
        try:
            recover_candidate_transition(sid)
        except BlockingIOError:
            # A running acquisition owns the worker lock; status is read-only.
            pass
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
