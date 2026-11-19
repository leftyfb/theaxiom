"""Versioned persistent source catalog; candidate URLs are data, never fetched."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import source_catalog

SCHEMA_VERSION = 1
EDITABLE = ("title", "description", "source_url", "publisher", "edition", "language",
            "expected_size_bytes", "sha256", "provenance", "license")
ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_CATALOG_BYTES = 256 * 1024
MAX_ENTRIES = 100
MAX_FIELD = 512
LOCK = threading.RLock()


class CatalogError(ValueError):
    pass


class RevisionConflict(CatalogError):
    pass


class CatalogStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read(self):
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "revision": 0, "updated_at": None,
                    "custom": {}, "overrides": {}, "tombstones": []}
        try:
            raw = self.path.read_bytes()
            if len(raw) > MAX_CATALOG_BYTES:
                raise CatalogError("catalog exceeds size limit")
            value = json.loads(raw)
            if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                    or value["schema_version"] != SCHEMA_VERSION
                    or type(value.get("revision")) is not int or value["revision"] < 0
                    or not isinstance(value.get("custom"), dict)
                    or not isinstance(value.get("overrides"), dict)
                    or not isinstance(value.get("tombstones"), list)):
                raise CatalogError("invalid catalog schema")
            for group in (value["custom"], value["overrides"]):
                for sid, fields in group.items():
                    self._validate_id(sid)
                    if not isinstance(fields, dict) or set(fields) - set(EDITABLE):
                        raise CatalogError("invalid catalog fields")
                    for key, val in fields.items():
                        self._validate_field(key, val)
            builtins = {x["source_id"] for x in source_catalog.builtin_sources()}
            for sid, fields in value["custom"].items():
                if sid in builtins or "title" not in fields:
                    raise CatalogError("invalid custom source entry")
            if any(sid not in builtins for sid in value["overrides"]):
                raise CatalogError("invalid built-in source override")
            if (len(value["tombstones"]) != len(set(value["tombstones"]))
                    or any(sid not in builtins for sid in value["tombstones"])):
                raise CatalogError("invalid source tombstones")
            if len(value["custom"]) + len(value["overrides"]) > MAX_ENTRIES:
                raise CatalogError("catalog entry limit exceeded")
            return value
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise CatalogError("catalog cannot be read safely") from exc

    @staticmethod
    def _validate_id(sid):
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
            raise CatalogError("invalid source ID")

    @staticmethod
    def _validate_field(key, value):
        if not isinstance(value, str) or len(value) > MAX_FIELD or any(ord(c) < 32 for c in value):
            raise CatalogError("invalid source field")
        if key == "title" and not value.strip():
            raise CatalogError("title is required")
        if key == "expected_size_bytes" and value and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise CatalogError("expected size must be a positive decimal byte count")
        if key == "sha256" and value and not re.fullmatch(r"[a-fA-F0-9]{64}", value):
            raise CatalogError("SHA-256 must contain exactly 64 hexadecimal characters")
        if key == "source_url" and value:
            try:
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path.startswith("//"):
                    raise ValueError
                if any(part in {".", ".."} for part in parsed.path.split("/")):
                    raise ValueError
                parsed.port
            except ValueError as exc:
                raise CatalogError("source URL must be a safe HTTP(S) URL") from exc

    def _snapshot(self):
        state = self._read()
        builtins = {x["source_id"]: x for x in source_catalog.builtin_sources()}
        result = []
        for sid, entry in builtins.items():
            if sid in state["tombstones"]:
                continue
            merged = copy.deepcopy(entry)
            override = state["overrides"].get(sid, {})
            for key, value in override.items():
                if key in {"source_url", "sha256", "expected_size_bytes", "license"}:
                    merged["candidate_" + key] = value
                else:
                    merged[key] = value
            merged.update(source_id=sid, builtin=True, candidate_only=bool(override))
            result.append(merged)
        for sid, fields in state["custom"].items():
            if sid not in builtins:
                result.append(dict(fields, source_id=sid, builtin=False, candidate_only=True))
        return {"revision": state["revision"], "updated_at": state["updated_at"], "sources": result}

    def get(self):
        with LOCK:
            fd = self._file_lock()
            try:
                return self._snapshot()
            finally:
                os.close(fd)

    def mutate(self, operation, source_id, fields, revision, busy=False):
        self._validate_id(source_id)
        if type(revision) is not int or revision < 0:
            raise CatalogError("revision precondition required")
        if busy:
            raise CatalogError("source has active or unresolved acquisition")
        if not isinstance(fields, dict) or set(fields) - set(EDITABLE):
            raise CatalogError("invalid editable fields")
        for key, value in fields.items():
            self._validate_field(key, value)
        with LOCK:
            fd = self._file_lock()
            try:
                state = self._read()
                if state["revision"] != revision:
                    raise RevisionConflict("catalog revision changed")
                builtins = {x["source_id"]: x for x in source_catalog.builtin_sources()}
                if operation == "add":
                    if source_id in builtins or source_id in state["custom"] or source_id in state["tombstones"]:
                        raise CatalogError("source ID already exists")
                    if "title" not in fields:
                        raise CatalogError("title is required")
                    state["custom"][source_id] = fields
                elif operation == "edit":
                    target = "overrides" if source_id in builtins else "custom"
                    if source_id not in builtins and source_id not in state["custom"]:
                        raise CatalogError("source not found")
                    state[target].setdefault(source_id, {}).update(fields)
                elif operation == "remove":
                    if source_id in builtins:
                        state["overrides"].pop(source_id, None)
                        state["tombstones"] = sorted(set(state["tombstones"]) | {source_id})
                    elif state["custom"].pop(source_id, None) is None:
                        raise CatalogError("source not found")
                else:
                    raise CatalogError("unknown operation")
                state["revision"] += 1
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                if len(payload) > MAX_CATALOG_BYTES or len(state["custom"]) + len(state["overrides"]) > MAX_ENTRIES:
                    raise CatalogError("catalog limit exceeded")
                self._atomic_write(payload)
                return self._snapshot()
            finally:
                os.close(fd)

    def _atomic_write(self, payload):
        fd, name = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            try:
                dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
