"""Durable ZIM jobs with concurrent source acquisition, explicit controls, and restart recovery."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import sqlite3
import os
import re
from pathlib import Path
import stat
import tempfile
import threading
import time
from uuid import uuid4

from . import acquisition_queue, catalog_store, storage_settings, zim_source, pdf_source, pdf_transport, source_activity

HISTORY_LIMIT = 12
MAX_RETRIES = 3
RECORD_OPEN_ATTEMPTS = 3

ACTIVE = {"queued", "downloading", "retrying", "verifying", "pausing", "cancelling", "publishing"}
CANDIDATE_ACTIVE = {"downloading", "retrying", "verifying", "cancelling"}
CHECKSUM_FIELDS = ("checksum_bytes", "checksum_pass", "checksum_passes")
CHECKSUM_SAMPLE_MAX_AGE = 10
CONTROLS = {}
CANDIDATE_CONTROLS = {}
LOCK = threading.RLock()


def _record_checksum(job, metadata):
    job.update({field: metadata[field] for field in CHECKSUM_FIELDS})
    job["checksum_sampled_at"] = time.time()


def current_checksum(job):
    """Project a recent verification sample without exposing its timestamp."""
    sampled = job.get("checksum_sampled_at")
    if (job.get("state") == "verifying" and type(sampled) in (int, float)
            and 0 <= time.time() - sampled <= CHECKSUM_SAMPLE_MAX_AGE
            and all(field in job for field in CHECKSUM_FIELDS)):
        return {field: job[field] for field in CHECKSUM_FIELDS}
    return {}


def directory():
    return storage_settings.SETTINGS_PATH.with_name("zim-jobs")


def path_for(sid):
    catalog_store.CatalogStore._validate_id(sid)
    return directory() / (sid + ".json")



def candidate_path_for(sid):
    """Replacement metadata is separate from the installed serving record."""
    catalog_store.CatalogStore._validate_id(sid)
    return directory() / (sid + ".candidate.json")


def _read_record_bytes(target, kind):
    for attempt in range(RECORD_OPEN_ATTEMPTS):
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            if attempt == 0:
                return None
            raise ValueError(f"Unsafe ZIM {kind} record") from exc
        try:
            info = os.fstat(fd)
            # Atomic replacement can unlink the inode between open and fstat.
            # Discard that descriptor without reading and validate a fresh one.
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 0:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Unsafe ZIM {kind} record")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read(16385)
        finally:
            os.close(fd)
    raise ValueError(f"Unsafe ZIM {kind} record")


def read_candidate(sid):
    """Read candidate metadata without modifying the installed archive."""
    raw = _read_record_bytes(candidate_path_for(sid), "candidate")
    if raw is None:
        return None
    if len(raw) > 16384:
        raise ValueError("ZIM candidate record exceeds size limit")
    try:
        candidate = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid ZIM candidate record") from exc
    if (not isinstance(candidate, dict) or candidate.get("source_id") != sid
            or candidate.get("state") not in CANDIDATE_ACTIVE | {"prepared", "interrupted", "cancelled", "failed", "verified", "activated", "rolled_back"}
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
    if not candidate or candidate["state"] not in CANDIDATE_ACTIVE:
        return candidate
    with LOCK:
        try:
            with worker_lock(source_id=sid):
                candidate = read_candidate(sid)
                if candidate and candidate["state"] in CANDIDATE_ACTIVE:
                    candidate["restart_pending"] = candidate["state"] != "cancelling"
                    candidate["state"] = "interrupted"
                    candidate["error"] = "Replacement worker stopped; staged bytes retained for review"
                    _save_candidate(candidate)
        except BlockingIOError:
            return candidate
    return candidate


def prepare_candidate(sid, config, *, replace_activated=False, automatic_job=None, supersede_rollback=None):
    """Persist replacement intent only; do not download or activate it."""
    configuration = identity(config)
    with LOCK:
        with worker_lock(source_id=sid):
            live = read(sid)
            if (not live or live.get("state") != "completed"
                    or live.get("verified") is not True
                    or not installed(live, live["configuration"])):
                raise ValueError("A verified installed archive is required for replacement")
            if configuration == live["configuration"]:
                raise ValueError("Replacement candidate must differ from installed archive")
            target = candidate_path_for(sid)
            previous = read_candidate(sid) if replace_activated else None
            can_replace = (previous and previous["state"] == "activated"
                           and live["artifact"] == previous["artifact"]
                           and live["configuration"] == previous["configuration"])
            if supersede_rollback is not None:
                if (not previous or previous["state"] != "rolled_back" or live != previous["previous_job"]
                        or live["artifact"] != previous["base_artifact"]
                        or configuration == previous["configuration"] or rollback_token(previous) != supersede_rollback):
                    raise ValueError("The rollback changed; review the replacement again")
                can_replace = True  # Keep both archives; replace only the candidate record.
            if target.is_symlink() or (target.exists() and not can_replace):
                raise ValueError("Replacement candidate already exists")
            candidate = {"source_id": sid, "state": "prepared",
                         "configuration": configuration, "base_artifact": live["artifact"],
                         "created_at": time.time()}
            if automatic_job is not None:
                candidate["automatic_job"] = automatic_job
            if can_replace:
                _save_candidate(candidate)
                return candidate
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
    with worker_lock(source_id=sid):
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
            staging_id=replacement_id, write_lock=_write_lock)
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


def start_candidate_acquisition(sid, config, *, automatic_job=None, activity_trigger=None, recovering=False):
    """Queue a replacement download without blocking the HTTP request."""
    configuration = identity(config)
    with LOCK:
        guard = worker_lock(source_id=sid)
        guard.__enter__()
        try:
            candidate = read_candidate(sid)
            live = read(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if candidate["state"] not in {"prepared", "failed", "interrupted", "cancelled"}:
                raise ValueError("Replacement candidate is not ready for acquisition")
            if recovering and (candidate["state"] != "interrupted" or not candidate.get("restart_pending")):
                raise ValueError("No interrupted replacement awaiting recovery")
            if automatic_job is None:
                candidate.pop("automatic_job", None)
            elif candidate.get("automatic_job") != automatic_job:
                raise ValueError("Replacement is not owned by this automatic attempt")
            if (not live or live.get("state") != "completed"
                    or live.get("artifact") != candidate["base_artifact"]
                    or not installed(live, live["configuration"])):
                raise ValueError("Installed archive changed; replacement requires review")
            staging_id = sid + "-candidate"
            catalog_store.CatalogStore._validate_id(staging_id)
            offset = staged_size(staging_id)
            validator = pdf_transport.resume_validator(candidate.get("etag"), candidate.get("last_modified")) if offset else None
            restart_from = 0
            if recovering and not offset and candidate.get("downloaded_bytes"):
                raise ValueError("Previously retained replacement bytes are missing")
            if offset and not validator and candidate["state"] == "failed" and candidate.get("retry_exhausted") and offset == candidate.get("downloaded_bytes"):
                restart_from, offset = offset, 0
            elif offset and not validator:
                raise ValueError("Cannot safely resume without publisher validator; cancel and discard staging first")
            if not offset:
                # A fresh transfer must not inherit validators from an earlier
                # failed attempt; those headers describe a different response.
                candidate.pop("etag", None)
                candidate.pop("last_modified", None)
            with (pdf_source._directory("staging", writing=True, format_name="zim") as (sfd, sm, _),
                  pdf_source._directory("library", writing=True, format_name=configuration["format"]) as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                need = max(96, int(configuration["expected_size_bytes"] or 0) - offset)
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve + need:
                    raise ValueError("Insufficient writable archive storage capacity")
            candidate.pop("restart_pending", None)
            candidate["restart_recovery"] = recovering
            candidate.update(state="downloading", automatic_retry_count=candidate.get("automatic_retry_count", 0) if recovering else 0,
                             transfer_activity_id=uuid4().hex,
                             transfer_activity_trigger=activity_trigger or (
                                 ("scheduled" if config.get("update_policy") == "scheduled" else "automatic")
                                 if automatic_job else "manual"))
            candidate.pop("error", None)
            candidate.pop("retry_delay_seconds", None)
            candidate.pop("retry_exhausted", None)
            candidate["downloaded_bytes"] = offset
            signal = threading.Event()
            CANDIDATE_CONTROLS[sid] = signal
            _save_candidate(candidate)
            if not automatic_job and candidate.get("transfer_activity_trigger") == "manual":
                try:
                    source_activity.record(sid, "update_started", key=source_activity.event_id(
                        sid, candidate["transfer_activity_id"], "manual_update_started"))
                except (OSError, sqlite3.Error):
                    logging.getLogger(__name__).warning("Could not journal replacement start")
            try:
                threading.Thread(target=_run_candidate, args=(sid, configuration, guard, signal, offset, validator),
                                 kwargs={"restart_from": restart_from} if restart_from else {},
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


def _retry_archive(transfer, staging_id, signal, offset, validator, load, persist, *, restart_from=0) -> dict:
    """Keep retries within the existing worker lock and matching staging file."""
    settings = storage_settings.load_settings()
    retrying = False
    prior_retries = load().get("automatic_retry_count", 0)

    def attempt():
        if not retrying:
            return transfer(offset, validator, **({"restart_from": restart_from} if restart_from else {}))
        with LOCK:
            if signal.is_set():
                raise zim_source.TransferStopped()
            if storage_settings.load_settings() != settings:
                raise ValueError("ZIM storage settings changed during retry")
            job = load()
            retained = staged_size(staging_id)
            if retained != job["downloaded_bytes"]:
                raise ValueError("Staged ZIM size changed during retry backoff")
            stable = pdf_transport.resume_validator(job.get("etag"), job.get("last_modified")) if retained else None
            if retained and not stable and job.get("restart_recovery"):
                raise ValueError("Safe resume requires a publisher validator; partial retained")
            job.update(state="downloading", speed_bytes_per_second=0)
            job.pop("retry_delay_seconds", None)
            persist(job)
        return transfer(retained if stable else 0, stable,
                        **({"restart_from": retained} if retained and not stable else {}))

    def on_retry(count, delay, _error):
        nonlocal retrying
        with LOCK:
            if signal.is_set():
                raise zim_source.TransferStopped()
            if storage_settings.load_settings() != settings:
                raise ValueError("ZIM storage settings changed during retry")
            job = load()
            job.update(state="retrying", automatic_retry_count=prior_retries + count, retry_delay_seconds=delay,
                       downloaded_bytes=staged_size(staging_id), sampled_at=time.time())
            persist(job)
            retrying = True

    return pdf_transport.run_with_download_retries(attempt, stopped=signal.is_set,
                                                    on_retry=on_retry, wait=signal.wait, retries_used=prior_retries)


def _run_candidate(sid, configuration, guard, signal, offset, validator, *, restart_from=0):
    last = 0.0

    def load():
        candidate = read_candidate(sid)
        if not candidate or candidate["configuration"] != configuration:
            raise ValueError("Replacement candidate changed during acquisition")
        return candidate

    def progress(current, total, metadata):
        nonlocal last
        now = time.monotonic()
        with LOCK:
            candidate = read_candidate(sid)
            if not candidate or candidate["state"] not in CANDIDATE_ACTIVE:
                return
            validator_changed = any(key in metadata and metadata[key] != candidate.get(key)
                                    for key in ("etag", "last_modified"))
            if now - last < 0.5 and not metadata.get("verifying") and not validator_changed:
                return
            candidate["downloaded_bytes"] = current
            if total is not None:
                candidate["expected_bytes"] = total
            _update_validators(candidate, metadata)
            if metadata.get("verifying") and candidate["state"] != "cancelling":
                # A verification callback must not erase a pending stop request.
                candidate["state"] = "verifying"
                if "checksum_bytes" in metadata:
                    _record_checksum(candidate, metadata)
            _save_candidate(candidate)
        last = now

    def transfer(start, stable, **kwargs):
        nonlocal last
        last = 0.0
        return zim_source.download_resumable(
            sid, configuration, offset=start, validator=stable, promote_lock=LOCK,
            staging_id=sid + "-candidate", progress=progress, stopped=signal.is_set, write_lock=_write_lock, **kwargs)

    try:
        artifact = _retry_archive(transfer, sid + "-candidate", signal, offset, validator, load, _save_candidate,
                                  restart_from=restart_from)
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
    except (zim_source.TransferStopped, acquisition_queue.Cancelled):
        with LOCK:
            candidate = read_candidate(sid)
            if candidate and candidate["configuration"] == configuration:
                candidate["state"] = "cancelled"
                candidate.pop("retry_delay_seconds", None)
                candidate["error"] = "Replacement download cancelled; staged bytes retained"
                _save_candidate(candidate)
    except Exception as exc:
        with LOCK:
            candidate = read_candidate(sid)
            if candidate and candidate["configuration"] == configuration:
                candidate["state"] = "failed"
                candidate["retry_exhausted"] = isinstance(exc, pdf_transport.RetryableDownloadError)
                candidate.pop("retry_delay_seconds", None)
                try:
                    candidate["downloaded_bytes"] = staged_size(sid + "-candidate")
                except (OSError, ValueError):
                    pass
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
        if candidate["state"] not in CANDIDATE_ACTIVE:
            raise ValueError("Replacement download is not running")
        signal = CANDIDATE_CONTROLS.get(sid)
        if signal is None:
            raise ValueError("Replacement worker belongs to another process")
        candidate["state"] = "cancelling"
        _save_candidate(candidate)
        signal.set()
        return {"accepted": True, "source_id": sid, "state": "cancelling"}


def rollback_token(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()


def activate_candidate(sid, config, *, rollback_retry=None, activity_trigger=None):
    """Switch the serving record only after the replacement artifact is installed.

    The old content-addressed archive remains on disk. Candidate metadata is
    retained for a subsequent explicit rollback operation.
    """
    configuration = identity(config)
    from . import publication
    with publication.LOCK, LOCK:
        with worker_lock(source_id=sid):
            candidate = read_candidate(sid)
            if not candidate or candidate["configuration"] != configuration:
                raise ValueError("No matching replacement candidate")
            if rollback_retry is not None:
                previous_job, token, _ = rollback_retry
                if (candidate["state"] != "rolled_back" or candidate.get("automatic_job") != previous_job
                        or candidate.get("error") or rollback_token(candidate) != token
                        or read(sid) != candidate["previous_job"]):
                    raise ValueError("The rollback changed; activation refused")
            elif candidate["state"] != "verified":
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
            candidate["activity_id"] = uuid4().hex
            candidate["activity_trigger"] = activity_trigger or (
                ("scheduled" if config.get("update_policy") == "scheduled" else "automatic")
                if candidate.get("automatic_job") or rollback_retry else "manual")
            candidate["activity_name"] = source_activity.source_name(sid)
            candidate["activity_old_edition"] = _activity_edition(live)
            candidate["activity_new_edition"] = _activity_edition(replacement) or config.get("edition")
            if rollback_retry is not None:
                candidate["automatic_job"] = rollback_retry[2]
            _save_candidate(candidate)
            save(replacement)
            if not publication.load().get(sid) or config.get("format") != "zim":
                confirm_activation(sid, config)
            return replacement


def rollback_candidate(sid, config, *, reason=None):
    """Restore the previous verified serving record without removing either archive."""
    if reason is not None and (not isinstance(reason, str) or not reason
                               or len(reason) > 240 or any(ord(ch) < 32 for ch in reason)):
        raise ValueError("Invalid replacement rollback reason")
    configuration = identity(config)
    with LOCK:
        with worker_lock(source_id=sid):
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
            candidate["rollback_id"] = uuid4().hex
            if reason is not None:
                candidate["error"] = reason
            _save_candidate(candidate)
            save(previous, activity=_rollback_activity(candidate, reason))

            return previous


def _activity_edition(job):
    from . import kiwix_update_discovery
    return (kiwix_update_discovery.installed_identity_from_job(job, job.get("artifact")) or {}).get("edition")


def _rollback_activity(candidate, reason=None):
    sid = candidate["source_id"]
    return dict(sid=sid, name=candidate.get("activity_name") or source_activity.source_name(sid),
                action="rolled_back", key=source_activity.event_id(sid, candidate["rollback_id"]),
                old_edition=candidate.get("activity_new_edition"), new_edition=candidate.get("activity_old_edition"),
                outcome="failed" if reason else "completed", trigger="automatic" if reason else "manual")


def confirm_activation(sid, config, *, trigger=None):
    with LOCK:
        candidate = read_candidate(sid)
        if not candidate or candidate["state"] != "activated" or candidate["configuration"] != identity(config):
            return
        if not candidate.get("activity_id"):
            return  # pre-journal deployment: do not fabricate historical events
        current = read(sid)
        if not current or current.get("artifact") != candidate["artifact"] or current.get("state") != "completed":
            return
        try:
            source_activity.record(sid, "updated", name=candidate.get("activity_name"),
                                   trigger=trigger or candidate.get("activity_trigger", "unknown"),
                                   old_edition=candidate.get("activity_old_edition"),
                                   new_edition=candidate.get("activity_new_edition"),
                                   key=source_activity.event_id(sid, candidate["activity_id"]))
        except (OSError, sqlite3.Error):
            # Keep the durable candidate activity ID for a later reconciliation.
            # A journal write error is not a failed reader activation.
            logging.getLogger(__name__).warning("Could not record committed activation activity")


def recover_candidate_transition(sid):
    """Recover interrupted activation/rollback without guessing which archive to serve.

    Activation writes its durable intent before changing the live job. Rollback
    writes the live job first. Reconcile only when the observed job matches
    exactly one of those two expected records.
    """
    from . import publication
    with publication.LOCK, LOCK:
        with worker_lock(source_id=sid):
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
                    if not publication.load().get(sid) or candidate["configuration"].get("format") != "zim":
                        confirm_activation(sid, candidate["configuration"])
                    return False
                if current != previous:
                    raise ValueError("Serving record diverged; recovery refused")
                if not installed(replacement, candidate["configuration"]):
                    raise ValueError("Replacement archive missing; recovery refused")
                save(replacement)
                if not publication.load().get(sid) or candidate["configuration"].get("format") != "zim":
                    confirm_activation(sid, candidate["configuration"])
                return True
            if current == previous:
                return False
            if current != replacement:
                raise ValueError("Serving record diverged; recovery refused")
            if not installed(previous, previous["configuration"]):
                raise ValueError("Previous archive missing; recovery refused")
            save(previous, activity=_rollback_activity(candidate, candidate.get("error")) if candidate.get("rollback_id") else None)
            return True


def _save_candidate(candidate):
    target = candidate_path_for(candidate["source_id"])
    payload = json.dumps(candidate, separators=(",", ":")).encode()
    action = {"verified":"staged", "failed":"download_failed", "cancelled":"download_cancelled"}.get(candidate["state"])
    event = action and candidate.get("transfer_activity_id")
    if event:
        sid = candidate["source_id"]
        source_activity.prepare_write(target, payload, sid=sid, name=source_activity.source_name(sid),
            action=action, outcome={"failed":"failed", "cancelled":"cancelled"}.get(candidate["state"], "completed"),
            trigger=candidate.get("transfer_activity_trigger", "unknown"),
            key=source_activity.event_id(sid, event, action))
    elif source_activity.path().exists():
        source_activity.reconcile_write(target)
    fd, name = tempfile.mkstemp(prefix=".candidate-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload.decode())
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

    if event:
        source_activity.reconcile_write(target)


def discard_candidate(sid, config):
    """Retire safe inactive candidate metadata without deleting archive bytes.

    An activated candidate is recovery-critical and cannot be discarded.
    A rolled-back candidate may be retired only after verifying the original
    serving record is restored, enabling a future replacement attempt.
    """
    configuration = identity(config)
    with LOCK:
        with worker_lock(source_id=sid):
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


def delete_verified_candidate(sid, config, digest, *, restore_profile):
    """Delete only the verified replacement that the operator reviewed."""
    with LOCK, worker_lock(source_id=sid):
        candidate = read_candidate(sid)
        if (not candidate or candidate["state"] not in {"verified", "rolled_back"}
                or candidate["artifact"].get("sha256") != digest):
            raise ValueError("Verified update changed. Reload and review before deleting.")
        live = read(sid)
        if (not live or live.get("artifact") != candidate["base_artifact"]
                or (candidate["state"] == "rolled_back" and live != candidate["previous_job"])
                or not installed(live, live["configuration"])):
            raise ValueError("Serving archive could not be verified.")
        configured = identity(config)
        serving = live["configuration"]
        if configured not in (candidate["configuration"], serving):
            raise ValueError("Download settings changed. Review them before deleting the update.")
        artifact = candidate["artifact"]
        if artifact["filename"] == live["artifact"]["filename"]:
            raise ValueError("Staged update overlaps the serving archive.")
        target = candidate_path_for(sid)
        with pdf_source._directory("library", writing=True, format_name=candidate["configuration"]["format"]) as (directory, mount, _):
            try:
                with open_installed_archive(directory, mount, candidate, artifact) as stream:
                    pdf_source.unlink_managed_file(directory, stream, artifact["filename"])
            except FileNotFoundError:
                # A previous attempt may have unlinked the bytes before its ledger write.
                pass
            os.fsync(directory)
        restore_profile(live)
        target.unlink()
        dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return {"accepted": True, "source_id": sid, "state": "discarded"}



@contextmanager
def downloaded_zim(sid):
    """Hold the acquisition locks and exact managed archive open for deletion."""
    from . import publication
    with publication.LOCK, LOCK, worker_lock(source_id=sid):
        if read_candidate(sid) is not None:
            raise ValueError("Retire the replacement candidate before deleting installed content")
        job = read(sid)
        if not job or job.get("state") != "completed" or job.get("active") is not False:
            raise ValueError("No inactive completed ZIM installation to delete")
        artifact = installed(job, job["configuration"])
        if not artifact:
            raise ValueError("Installed ZIM is missing or invalid; deletion refused")
        name, size = artifact["filename"], artifact["bytes"]
        with pdf_source._directory("library", writing=True, format_name=job["configuration"]["format"]) as (directory, mount, _):
            with open_installed_archive(directory, mount, job, artifact) as stream:
                yield directory, [(name, stream, size)]


def forget_deleted_installation(sid):
    target = path_for(sid)
    target.unlink()
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def delete_installed(sid):
    """Compatibility action for explicitly deleting one unpublished ZIM."""
    from . import publication
    with publication.LOCK:
        if publication.load().get(sid, False):
            raise ValueError("Unpublish the source before deleting installed content")
        with downloaded_zim(sid) as (directory, files):
            (name, stream, _), = files
            activity_key = source_activity.prepare_delete(directory, [name], sid=sid)
            pdf_source.unlink_managed_file(directory, stream, name)
            forget_deleted_installation(sid)
            os.fsync(directory)
    source_activity.finish_delete(activity_key)
    return {"accepted": True, "source_id": sid, "state": "deleted"}


def read(sid):
    """Read a single managed job record without following filesystem aliases.

    An untrusted symlink or hardlink must not be used as authority for archive
    replacement, publication, or explicit deletion.
    """
    raw = _read_record_bytes(path_for(sid), "job")
    if raw is None:
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
        history.append({"activity_id": uuid4().hex, "started_at": time.time(), "resume_offset": job.get("downloaded_bytes", 0),
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


def save(job, *, activity=None):
    target = path_for(job["source_id"])
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(job, separators=(",", ":")).encode()
    history = job.get("attempt_history", [])
    if history and activity is None:
        attempt = history[-1]
        action = {"completed":"downloaded","failed":"download_failed","cancelled":"download_cancelled"}.get(job["state"])
        if action and attempt.get("activity_id") and attempt.get("state") == job["state"] and attempt.get("operation") != "revalidate":
            sid = job["source_id"]
            activity = dict(sid=sid, name=source_activity.source_name(sid), action=action,
                            trigger=job.get("activity_trigger", "manual"),
                            outcome={"failed":"failed","cancelled":"cancelled"}.get(job["state"], "completed"),
                            key=source_activity.event_id(sid, attempt["activity_id"], action),
                            new_edition=_activity_edition(job) if action == "downloaded" else None)
    if activity:
        source_activity.prepare_write(target, payload, **activity)
    elif source_activity.path().exists():
        # Recover a committed intent before replacing this job record.
        source_activity.reconcile_write(target)
    fd, name = tempfile.mkstemp(prefix=".job-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
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
    if activity:
        source_activity.reconcile_write(target)


@contextmanager
def worker_lock(*, source_id=None):
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        mode = fcntl.LOCK_SH if source_id is not None else fcntl.LOCK_EX
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        if source_id is None:
            yield
            return
        catalog_store.CatalogStore._validate_id(source_id)
        # A replacement uses the staging identity <source>-candidate.
        # Serialize that namespace with any literal source ID that aliases it.
        lock_id = source_id
        while lock_id.endswith("-candidate") and lock_id != "-candidate":
            lock_id = lock_id.removesuffix("-candidate")
        source_fd = os.open(root / f".worker-{lock_id}.lock",
                            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(source_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(source_fd)
    finally:
        os.close(fd)


@contextmanager
def _write_lock():
    fd = os.open(directory() / ".storage-write.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _promoted_artifact(job):
    """Verify metadata for the exact inode certified by the interrupted worker."""
    try:
        intent = job["promotion_intent"]
        artifact = intent["artifact"]
        url, size, digest = zim_source.candidate(job["configuration"])
        name = artifact["filename"]
        checksum = artifact["sha256"]
        length = artifact["bytes"]
        if (not re.fullmatch("[a-f0-9]{64}", checksum)
                or name != f'{job["source_id"]}.{checksum}.{job["configuration"]["format"]}'
                or artifact["source_url"] != url or (digest and checksum != digest)
                or type(length) is not int or not 96 <= length <= zim_source.MAX_BYTES
                or (size and length != size)):
            return None
        with pdf_source._directory("library", format_name=job["configuration"]["format"]) as (fd, mount, _):
            with pdf_source._file(fd, mount, name, length, allowed_links=(1, 2)) as stream:
                info = os.fstat(stream.fileno())
                if zim_source.promotion_identity(info) != intent["identity"]:
                    return None
                os.fsync(fd)
                if info.st_nlink == 2:
                    _remove_promotion_alias(job["source_id"], fd, mount, name, stream.fileno(), info)
        return artifact
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _remove_promotion_alias(sid, library, mount, filename, file_fd, info):
    """Remove only the expected staging name after securing the library link."""
    with pdf_source._directory("staging", format_name="zim") as (staging, staged_mount, _):
        if (mount != staged_mount or os.fstat(staging).st_mode & 0o022
                or os.fstat(library).st_mode & 0o022):
            raise ValueError("Storage directories are not safe for automatic alias cleanup")
        name = zim_source.partial_name(sid)
        with pdf_source._file(staging, mount, name, info.st_size, allowed_links=(2,)) as partial:
            staged = os.fstat(partial.fileno())
            current = os.stat(filename, dir_fd=library, follow_symlinks=False)
            expected = zim_source.promotion_identity(info)
            if (zim_source.promotion_identity(staged) != expected
                    or zim_source.promotion_identity(current) != expected
                    or current.st_nlink != 2):
                raise ValueError("Promotion links do not match the verified archive")
            os.unlink(name, dir_fd=staging)
            os.fsync(staging)
            final = os.fstat(file_fd)
            if final.st_nlink != 1 or zim_source.promotion_identity(final) != expected:
                raise ValueError("Recovered library link changed")


def _finish_orphaned_promotion(job, guard):
    try:
        artifact = _promoted_artifact(job)
        with LOCK:
            job.pop("promotion_intent", None)
            if artifact:
                job.update(state="completed", verified=True, artifact=artifact,
                           downloaded_bytes=artifact["bytes"], expected_bytes=artifact["bytes"],
                           detail="Recovered previously verified acquisition.")
            else:
                job.update(state="interrupted", verified=False,
                           detail="Promotion interrupted; review the library and retained files before restarting.")
            job.update(active=False, sampled_at=time.time())
            _record_attempt(job)
            save(job)
    finally:
        guard.__exit__(None, None, None)


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
        if job and job["state"] == "publishing" and "promotion_intent" in job:
            guard = worker_lock(source_id=sid)
            try:
                guard.__enter__()
            except BlockingIOError:
                return job
            try:
                threading.Thread(target=_finish_orphaned_promotion, args=(job, guard),
                                 daemon=True, name="zim-promotion-recovery").start()
            except BaseException:
                guard.__exit__(None, None, None)
                raise
            return job
        if job and job["state"] in ACTIVE:
            try:
                with worker_lock(source_id=sid):
                    job["restart_previous_bytes"] = job.get("downloaded_bytes", 0)
                    try:
                        job["downloaded_bytes"] = staged_size(sid)
                    except (OSError, ValueError):
                        job["downloaded_bytes"] = 0
                    pending = job["state"] in {"queued", "downloading", "retrying", "verifying"}
                    job.update(state="paused", active=False, restart_pending=pending,
                               detail="Backend restarted; awaiting safe resume." if pending else
                               "Backend restarted; explicit stop retained.")
                    save(job)
            except BlockingIOError:
                pass
        return job


def identity(config):
    url, size, digest = zim_source.candidate(config)
    result = {"format": config["format"], "source_url": url, "expected_size_bytes": str(size) if size else "",
              "sha256": digest or ""}
    if publisher_sha1 := zim_source.publisher_sha1(config):
        result["publisher_sha1"] = publisher_sha1
    if publisher_md5 := zim_source.publisher_md5(config):
        result["publisher_md5"] = publisher_md5
    return result


def preview_acquisition(sid, config, *, exact_selection=False):
    """Estimate a full ZIM transfer from catalog intent and recorded jobs only."""
    configured = identity(config)
    job = read(sid)
    replacement = read_candidate(sid)
    artifact = (job.get("artifact") if job and job["state"] == "completed"
                and job.get("verified") is True else None)
    recorded = artifact.get("bytes") if isinstance(artifact, dict) else None
    if type(recorded) is not int or not 0 < recorded <= zim_source.MAX_BYTES:
        recorded = 0 if job is None else None
    if replacement and (replacement["state"] in {"verified", "activated", "rolled_back"}
                        or (exact_selection and replacement["configuration"] != configured)):
        # A replacement may consume independent space or target another edition.
        return dict(state="review_replacement", installed_bytes=recorded)
    intended = replacement["configuration"] if replacement else configured
    if recorded and not replacement and job["configuration"] == intended:
        return dict(state="installed", installed_bytes=recorded, transfer_bytes=0,
                    staging_bytes=0, peak_bytes=recorded)
    transfer = int(intended["expected_size_bytes"]) if intended["expected_size_bytes"] else None
    # The ZIM worker promotes its staged inode by hard link. Full staging
    # accounts for a transfer even when a reusable partial could reduce it.
    return dict(state="estimated", installed_bytes=recorded, transfer_bytes=transfer,
                staging_bytes=transfer,
                peak_bytes=recorded + transfer if recorded is not None and transfer is not None else None)


def matches(job, config):
    try:
        return job is not None and job["configuration"] == identity(config)
    except ValueError:
        return False


def archive_identity(info):
    """Identity of a verified file whose directory entry is left untouched."""
    return dict(device=info.st_dev, inode=info.st_ino, bytes=info.st_size,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


@contextmanager
def open_installed_archive(directory, mount, job, artifact):
    """Open the verified, digest-named file recorded in the managed ledger."""
    digest = artifact["sha256"]
    filename = artifact["filename"]
    if not re.fullmatch("[a-f0-9]{64}", digest) or filename != f'{job["source_id"]}.{digest}.{job["configuration"]["format"]}':
        raise ValueError("Invalid installed archive metadata")
    with pdf_source._file(directory, mount, filename, artifact["bytes"]) as stream:
        yield stream


def installed(job, config):
    if not matches(job, config) or job["state"] != "completed":
        return None
    try:
        artifact = job["artifact"]
        with pdf_source._directory("library", format_name=job["configuration"]["format"]) as (fd, mount, _):
            with open_installed_archive(fd, mount, job, artifact):
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


def start(sid, config, *, resume=False, restart=False, retry=False, activity_trigger="manual", recovering=False):
    if retry and (resume or restart):
        raise ValueError("Retry cannot be combined with resume or restart")
    if restart and resume:
        raise ValueError("Restart and resume are mutually exclusive")
    configuration = identity(config)
    with LOCK:
        guard = worker_lock(source_id=sid)
        guard.__enter__()
        try:
            old = read(sid)
            if recovering:
                if not old or not old.get("restart_pending") or old["state"] != "paused" or not matches(old, config):
                    raise ValueError("No interrupted download awaiting recovery")
                retained = staged_size(sid)
                if not retained and old.get("restart_previous_bytes"):
                    raise ValueError("Previously retained bytes are missing; automatic restart refused")
                resume, restart = bool(retained), not retained
                if retained and not pdf_transport.resume_validator(old.get("etag"), old.get("last_modified")):
                    raise ValueError("Safe resume requires retained bytes and a publisher validator")
            # A changed source URL/checksum must not bypass the installed-content
            # guard: overwriting the completed job would orphan the published
            # archive before a replacement had been verified and activated.
            if old and old.get("state") == "completed":
                raise ValueError("Archive already installed; replacement is not yet supported.")
            if old and old.get("state") == "publishing" and "promotion_intent" in old:
                raise ValueError("A previous promotion is unresolved; check acquisition status before restarting.")
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
            restart_from = 0
            if resume or (retry and old and old.get("retry_exhausted")) or (retry and staged_size(sid) > 0):
                if not old or old["state"] not in {"paused", "failed", "interrupted"} or not matches(old, config):
                    raise ValueError("No matching paused ZIM download")
                offset = staged_size(sid)
                validator = pdf_transport.resume_validator(old.get("etag"), old.get("last_modified")) if offset else None
                if retry and old.get("retry_exhausted") and offset == old.get("downloaded_bytes") and not validator:
                    restart_from, offset = offset, 0
                    old.pop("etag", None)
                    old.pop("last_modified", None)
                elif not offset or not validator:
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
                  pdf_source._directory("library", writing=True, format_name=configuration["format"]) as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                need = max(96, (int(configuration["expected_size_bytes"] or 0) - offset))
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve + need:
                    raise ValueError("Insufficient writable ZIM storage capacity")
            job.update(state="queued", active=True, detail="", downloaded_bytes=offset, sampled_at=time.time())
            if recovering:
                job["automatic_retry_count"] = old.get("automatic_retry_count", 0)
                job["retry_count"] = previous_retries
                if job is not old:
                    job["attempt_history"] = list(old.get("attempt_history", []))
            else:
                job.update(automatic_retry_count=0)
            job.pop("restart_pending", None)
            job.pop("restart_previous_bytes", None)
            job["restart_recovery"] = recovering
            job.pop("retry_delay_seconds", None)
            job.pop("retry_exhausted", None)
            job["activity_trigger"] = activity_trigger
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
                             kwargs={"restart_from": restart_from} if restart_from else {},
                             daemon=True, name="zim-acquisition").start()
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return {"accepted": True, "source_id": sid, "state": "queued"}



def revalidate(sid, config):
    """Explicitly recheck a complete retained ZIM with no network request."""
    configuration = identity(config)
    with LOCK:
        guard = worker_lock(source_id=sid)
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
                  pdf_source._directory("library", writing=True, format_name=configuration["format"]) as (_, lm, reserve)):
                cap = os.fstatvfs(sfd)
                if sm != lm or cap.f_flag & os.ST_RDONLY or cap.f_bavail * cap.f_frsize < reserve:
                    raise ValueError("Insufficient writable ZIM storage capacity")
            old.pop("restart_pending", None)
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
    job.pop("restart_pending", None)
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
            with worker_lock(source_id=sid):
                return _retain_cancelled(job, sid)
        if action == "cancel" and job["state"] in {"paused", "failed", "interrupted"}:
            with worker_lock(source_id=sid):
                if staged_present(sid):
                    zim_source.discard_partial(sid)
                job.pop("restart_pending", None)
                job.update(state="cancelled", active=False, verified=False, downloaded_bytes=0,
                           detail="Download cancelled; partial removed.", sampled_at=time.time())
                save(job)
            return {"accepted": True, "state": "cancelled"}
        if action == "pause" and job["state"] == "paused":
            job.pop("restart_pending", None)
            save(job)
            return {"accepted": True, "state": "paused"}
        raise ValueError("ZIM job cannot be " + action + "d in current state")


def _update_validators(job, metadata):
    for key in ("etag", "last_modified"):
        if key in metadata:
            if metadata[key]:
                job[key] = metadata[key]
            else:
                job.pop(key, None)


def _run(job, guard, signal, offset, validator, *, local=False, restart_from=0):
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
            _update_validators(job, metadata)
            if total is not None:
                job["expected_bytes"] = total
            if metadata.get("verifying") and job["state"] not in {"pausing", "cancelling"}:
                job["state"] = "verifying"
                if "checksum_bytes" in metadata:
                    _record_checksum(job, metadata)
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
                job.pop("checksum_sampled_at", None)
                for field in CHECKSUM_FIELDS:
                    job.pop(field, None)
                job.update(state="verifying" if local else "downloading", sampled_at=time.time())
                save(job)
        def promoting(artifact, identity):
            job.update(state="publishing", sampled_at=time.time(),
                       promotion_intent={"artifact": artifact, "identity": identity})
            save(job)

        if local:
            artifact = zim_source.revalidate_retained(
                sid, job["configuration"], expected_bytes=offset,
                progress=progress, stopped=signal.is_set,
                promote_lock=LOCK, promoting=promoting)
        else:
            def transfer(start, stable, **kwargs):
                nonlocal offset, begun, last
                offset, begun, last = start, time.monotonic(), 0.0
                return zim_source.download_resumable(
                    sid, job["configuration"], offset=start,
                    validator=stable, progress=progress, stopped=signal.is_set,
                    promote_lock=LOCK, promoting=promoting, write_lock=_write_lock, **kwargs)
            artifact = _retry_archive(transfer, sid, signal, offset, validator, lambda: job, save,
                                      restart_from=restart_from)
        job.pop("promotion_intent", None)
        job.update(state="completed", active=False, verified=True, artifact=artifact,
                   downloaded_bytes=artifact["bytes"], expected_bytes=artifact["bytes"], sampled_at=time.time())
    except (zim_source.TransferStopped, acquisition_queue.Cancelled):
        job.update(state="paused", active=False, verified=False,
                   detail="Download paused; partial retained.", sampled_at=time.time())
    except Exception as exc:
        detail = str(exc)[:240] if isinstance(exc, ValueError) else "ZIM acquisition failed (network or storage I/O)."
        job.update(state="failed", active=False, verified=False, detail=detail, sampled_at=time.time())
        job["retry_exhausted"] = isinstance(exc, pdf_transport.RetryableDownloadError)
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
                job.pop("retry_delay_seconds", None)
                _record_attempt(job)
                save(job)
            finally:
                CONTROLS.pop(sid, None)
                guard.__exit__(None, None, None)
