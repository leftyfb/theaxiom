"""Durable, source-scoped update-check evidence separate from installed content.

No archive or catalog writes. Cached answers are bound to an exact installed
digest; changing active content invalidates a prior update notification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from . import storage_settings

MAX_RECORD = 4096
MAX_AGE = 48 * 60 * 60
STATES = frozenset({"update_available", "up_to_date", "older_candidate",
                    "unable_to_determine", "representation_changed",
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
               "candidate_url", "candidate_sha256", "candidate_bytes"}
    if set(result) - allowed:
        raise ValueError("Unexpected update-check fields")
    for field, value in result.items():
        if field == "candidate_bytes":
            if type(value) is not int or not 96 <= value <= 2**63 - 1:
                raise ValueError("Invalid candidate size")
            continue
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid update-check value")
    if result["state"] == "update_available":
        if not _digest(result.get("candidate_sha256")):
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


def save(source_id: str, installed_sha256: str, result: dict, *, now=None):
    """Atomically save one checked result; no update or download action."""
    stamp = time.time() if now is None else now
    data = _validated(source_id, installed_sha256, stamp, result)
    target = record_path(source_id)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(source_id: str, installed_sha256: str, *, now=None) -> dict:
    """Return an uncertain state if evidence is absent, corrupt, old or rebound."""
    unknown = {"state": "unable_to_determine", "evidence": "no current check evidence"}
    try:
        target = record_path(source_id)
        if not _digest(installed_sha256):
            return unknown
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > MAX_RECORD or info.st_mode & 0o077):
                return unknown
            with os.fdopen(fd, "rb", closefd=False) as stream:
                payload = stream.read(MAX_RECORD + 1)
        finally:
            os.close(fd)
        record = json.loads(payload)
        if (not isinstance(record, dict) or set(record) != {
                "source_id", "installed_sha256", "checked_at", "result"}
                or record["source_id"] != source_id
                or record["installed_sha256"] != installed_sha256):
            return unknown
        _validated(source_id, installed_sha256, record["checked_at"], record["result"])
        present = time.time() if now is None else now
        if (type(present) not in (int, float) or present < record["checked_at"]
                or present - record["checked_at"] > MAX_AGE):
            return unknown
        return dict(record["result"], checked_at=record["checked_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return unknown
