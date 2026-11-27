"""Durable, source-scoped update-check evidence separate from installed content.

No archive or catalog writes. Cached answers are bound to an exact installed
digest; changing active content invalidates a prior update notification.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from . import storage_settings

MAX_RECORD = 4096
MAX_AGE = 48 * 60 * 60
LOG = logging.getLogger(__name__)
STATES = frozenset({"update_available", "up_to_date", "older_candidate",
                    "unable_to_determine", "no_newer_edition_listed", "representation_changed",
                    "representation_unchanged"})


def root_path() -> Path:
    return storage_settings.SETTINGS_PATH.with_name("update-checks")


def record_path(source_id: str) -> Path:
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source_id):
        raise ValueError("Invalid source ID")
    return root_path() / (source_id + ".json")


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _validated(source_id: str, installed_sha256: str, checked_at: float, result: dict):
    record_path(source_id)
    if not _digest(installed_sha256):
        raise ValueError("Invalid installed SHA-256")
    if not isinstance(result, dict) or result.get("state") not in STATES:
        raise ValueError("Invalid update-check state")
    if type(checked_at) not in (int, float) or not 0 < checked_at < 4102444800:
        raise ValueError("Invalid update-check timestamp")
    allowed = {"state", "evidence", "installed_edition", "candidate_edition",
               "candidate_url", "candidate_sha256", "candidate_sha1", "candidate_md5", "candidate_bytes",
               "candidate_etag", "candidate_token"}
    if set(result) - allowed:
        raise ValueError("Unexpected update-check fields")
    for field, value in result.items():
        if field == "candidate_bytes":
            if type(value) is not int or not (1 if result["state"] == "representation_changed" else 96) <= value <= 2**63 - 1:
                raise ValueError("Invalid candidate size")
            continue
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid update-check value")
    if {"candidate_etag", "candidate_token"} & result.keys():
        if (result["state"] != "representation_changed" or not _digest(result.get("candidate_token"))
                or not re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', result.get("candidate_etag", ""))
                or not isinstance(result.get("candidate_url"), str) or not result["candidate_url"].startswith("https://")
                or type(result.get("candidate_bytes")) is not int or not 0 < result["candidate_bytes"] <= 16 * 1024 * 1024):
            raise ValueError("Invalid reviewed document representation")
    if result["state"] == "update_available":
        algorithms = {"candidate_sha256": 64, "candidate_sha1": 40, "candidate_md5": 32}
        digests = [(result[key], length) for key, length in algorithms.items() if key in result]
        if (len(digests) != 1 or not isinstance(digests[0][0], str)
                or not re.fullmatch(r"[a-f0-9]{%d}" % digests[0][1], digests[0][0])):
            raise ValueError("Update requires a candidate digest")
        if (not isinstance(result.get("candidate_url"), str)
                or not result["candidate_url"].startswith("https://")):
            raise ValueError("Update requires a HTTPS candidate")
    payload = {"source_id": source_id, "installed_sha256": installed_sha256,
               "checked_at": checked_at, "result": result}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(blob) > MAX_RECORD:
        raise ValueError("Oversized update-check record")
    return blob


def _write(target, data):
    if target.is_symlink():
        raise ValueError("Unsafe update-check record")
    fd, temporary = tempfile.mkstemp(prefix=".update-check-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_bytes(target):
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > MAX_RECORD or info.st_mode & 0o077):
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                payload = stream.read(MAX_RECORD + 1)
        finally:
            os.close(fd)
        return payload if len(payload) <= MAX_RECORD else None
    except OSError:
        return None


def _read(target, source_id, installed_sha256):
    try:
        payload = read_bytes(target)
        if payload is None:
            return None
        record = json.loads(payload)
        if (not isinstance(record, dict) or set(record) != {
                "source_id", "installed_sha256", "checked_at", "result"}
                or record["source_id"] != source_id
                or record["installed_sha256"] != installed_sha256):
            return None
        _validated(source_id, installed_sha256, record["checked_at"], record["result"])
        return record
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _history_path(target):
    return target.with_suffix(".last-success.json")


def pending_recheck_token(source_id):
    """Identify a pending publisher recheck without exposing cached publisher data."""
    marker = record_path(source_id).with_suffix(".recheck")
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_mtime_ns


def require_recheck(source_id):
    target = record_path(source_id).with_suffix(".recheck")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _write(target, b"fresh check required\n")
        os.fsync(fd)
    finally:
        os.close(fd)



def save(source_id: str, installed_sha256: str, result: dict, *, now=None, recheck_token=None):
    """Save the result and retain the last successful check on failure."""
    stamp = time.time() if now is None else now
    data = _validated(source_id, installed_sha256, stamp, result)
    target = record_path(source_id)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        pending = pending_recheck_token(source_id)
        if pending is not None and (pending != recheck_token or result["state"] == "unable_to_determine"):
            return
        previous = _read(target, source_id, installed_sha256)
        if previous and previous["checked_at"] > stamp:
            return
        if (result["state"] == "unable_to_determine" and previous
                and previous["result"]["state"] != "unable_to_determine"):
            try:
                _write(_history_path(target), _validated(source_id, installed_sha256,
                                                       previous["checked_at"], previous["result"]))
                os.fsync(directory_fd)
            except (OSError, ValueError):
                # Saving the current failure takes precedence over historical display.
                LOG.warning("Unable to retain last successful update-check evidence")
        _write(target, data)
        if pending is not None:
            target.with_suffix(".recheck").unlink()
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if result.get("state") == "update_available" or result.get("candidate_token"):
        from . import source_activity, update_diagnostics
        import sqlite3
        digest = result.get("candidate_sha256") or result.get("candidate_sha1") or result.get("candidate_md5") or result.get("candidate_token")
        if digest:
            try:
                source_activity.record(source_id, "update_discovered", trigger=update_diagnostics.current_trigger(),
                    old_edition=result.get("installed_edition"), new_edition=result.get("candidate_edition"),
                    key=source_activity.event_id("discovery", source_id, installed_sha256, digest))
            except (OSError, sqlite3.Error):
                LOG.warning("Unable to journal update discovery")



def load(source_id: str, installed_sha256: str, *, now=None) -> dict:
    """Return actionable evidence only from a current successful check."""
    unknown = {"state": "unable_to_determine", "evidence": "no current check evidence"}
    try:
        target = record_path(source_id)
        if pending_recheck_token(source_id) is not None:
            return unknown
    except (ValueError, OSError):
        return unknown
    if not _digest(installed_sha256):
        return unknown
    record = _read(target, source_id, installed_sha256)
    present = time.time() if now is None else now
    if not record or type(present) not in (int, float) or not present >= record["checked_at"]:
        return unknown
    stamp, result = record["checked_at"], record["result"]
    current = dict(result, checked_at=stamp) if present - stamp <= MAX_AGE else dict(
        unknown, evidence="last update check is stale", stale=True)
    success = record
    if result["state"] == "unable_to_determine":
        success = _read(_history_path(target), source_id, installed_sha256)
    if (success and success["checked_at"] <= stamp
            and success["result"]["state"] != "unable_to_determine"):
        current["last_successful_check_at"] = success["checked_at"]
        edition = success["result"].get("candidate_edition")
        if edition and current["state"] == "unable_to_determine":
            current["last_known_candidate_edition"] = edition
    return unknown if pending_recheck_token(source_id) is not None else current
