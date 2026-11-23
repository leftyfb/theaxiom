"""Versioned persistent source catalog; candidate URLs are data, never fetched."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
from collections.abc import Set
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import source_catalog

SCHEMA_VERSION = 2
PROFILE_FIELDS = {"title", "description", "publisher", "edition", "language", "author"}
CANDIDATE_FIELDS = {"source_url", "expected_size_bytes", "sha256", "provenance", "license"}
CONFIG_FIELDS = CANDIDATE_FIELDS | {"format"}
V1_FIELDS = PROFILE_FIELDS | CANDIDATE_FIELDS
EDITABLE = PROFILE_FIELDS | CONFIG_FIELDS
HEADER_FIELDS = {"schema_version", "revision", "updated_at"}
ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_CATALOG_BYTES = 256 * 1024
MAX_ENTRIES = 100
MAX_FIELD = 512
GROUP_KINDS = frozenset({'categories', 'collections', 'tags'})
MAX_GROUPS = 100
MAX_GROUP_NAME = 100
DEFAULT_CATEGORIES = (
    ("emergency-preparedness", "Emergency Preparedness"),
    ("survival-bushcraft", "Survival & Bushcraft"),
    ("health-medicine", "Health & Medicine"),
    ("food-agriculture", "Food & Agriculture"),
    ("water-sanitation", "Water & Sanitation"),
    ("science-education", "Science & Education"),
    ("technology-computing", "Technology & Computing"),
    ("engineering-repair", "Engineering & Repair"),
    ("energy-power", "Energy & Power"),
    ("communications", "Communications"),
    ("maps-navigation", "Maps & Navigation"),
    ("history-culture", "History & Culture"),
    ("general-reference", "General Reference"),
)
DEFAULT_TAGS = (
    ("first-aid", "First aid"),
    ("cpr", "CPR"),
    ("water-purification", "Water purification"),
    ("drinking-water", "Drinking water"),
    ("power-outages", "Power outages"),
    ("solar-power", "Solar power"),
    ("batteries", "Batteries"),
    ("generators", "Generators"),
    ("food-preservation", "Food preservation"),
    ("canning", "Canning"),
    ("gardening", "Gardening"),
    ("foraging", "Foraging"),
    ("cooking", "Cooking"),
    ("fire-starting", "Fire starting"),
    ("shelter", "Shelter"),
    ("knots", "Knots"),
    ("woodworking", "Woodworking"),
    ("electronics", "Electronics"),
    ("linux", "Linux"),
    ("networking", "Networking"),
    ("amateur-radio", "Amateur radio"),
    ("navigation", "Navigation"),
    ("weather", "Weather"),
    ("winter", "Winter"),
    ("floods", "Floods"),
    ("hurricanes", "Hurricanes"),
    ("earthquakes", "Earthquakes"),
    ("evacuation", "Evacuation"),
    ("bug-in", "Bug-in"),
    ("bug-out", "Bug-out"),
    ("beginner", "Beginner"),
    ("advanced", "Advanced"),
    ("field-manual", "Field manual"),
    ("quick-reference", "Quick reference"),
    ("step-by-step", "Step-by-step"),
)
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
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read(self, *, initialize=False) -> dict[str, Any]:
        # Both locks are held. A mutation persists seeding or conversion only
        # if the revision check and the requested change also succeed.
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_CATALOG_BYTES + 1)
        except FileNotFoundError:
            if self.path.is_symlink():
                raise CatalogError("catalog cannot be read safely")
            state = {"schema_version": SCHEMA_VERSION, "revision": 0, "updated_at": None,
                     "sources": [self._seed_record(entry) for entry in source_catalog.builtin_sources()],
                     "groups": self._default_groups(), "starter_groups_initialized": True}
        except OSError as exc:
            raise CatalogError("catalog cannot be read safely") from exc
        else:
            if len(raw) > MAX_CATALOG_BYTES:
                raise CatalogError("catalog exceeds size limit")
            try:
                state = json.loads(raw, object_pairs_hook=self._unique_object)
            except CatalogError:
                raise
            except (ValueError, RecursionError) as exc:
                raise CatalogError("catalog cannot be read safely") from exc
            self._validate_header(state)
            if state["schema_version"] == SCHEMA_VERSION:
                self._validate_collection(state)
                if not any("seeded" in record for record in state["sources"] if isinstance(record, dict)):
                    self._validate_v2(state)
                    # Existing catalogs are authoritative. Shipped seed changes
                    # never alter operator records, even if they still match
                    # a prior packaged default.
                    return state
                else:
                    state = self._convert_draft_v2(state)
            elif state["schema_version"] == 1:
                entries = {entry["source_id"]: entry for entry in source_catalog.builtin_sources()}
                self._validate_v1(state, entries)
                state = self._convert_v1(state, entries)
            else:
                raise CatalogError("invalid catalog schema")

        if not state.get("starter_groups_initialized"):
            self._merge_starter_groups(state)
            state["starter_groups_initialized"] = True
        payload = self._validated_payload(state)
        if initialize:
            self._atomic_write(payload)
        return state

    @staticmethod
    def _default_groups():
        return [dict(kind=kind, group_id=gid, name=name, source_ids=[])
                for kind, defaults in (("categories", DEFAULT_CATEGORIES), ("tags", DEFAULT_TAGS))
                for gid, name in defaults]

    @classmethod
    def _merge_starter_groups(cls, state):
        groups = state.setdefault("groups", [])
        keys = {(g["kind"], g["group_id"]) for g in groups}
        names = {(g["kind"], g["name"].casefold()) for g in groups}
        for default in cls._default_groups():
            if len(groups) >= MAX_GROUPS:
                break
            if ((default["kind"], default["group_id"]) not in keys
                    and (default["kind"], default["name"].casefold()) not in names):
                groups.append(default)

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogError("duplicate catalog key")
            result[key] = value
        return result

    @staticmethod
    def _validate_header(state):
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or type(state.get("revision")) is not int or state["revision"] < 0):
            raise CatalogError("invalid catalog schema")
        timestamp = state.get("updated_at")
        if timestamp is not None:
            try:
                if not isinstance(timestamp, str) or datetime.fromisoformat(timestamp).utcoffset() is None:
                    raise ValueError
                timestamp.encode("utf-8")
            except ValueError as exc:
                raise CatalogError("invalid catalog timestamp") from exc

    def _validate_v1(self, state, entries):
        legacy_fields = {"custom", "overrides", "tombstones"}
        if not legacy_fields <= state.keys() or state.keys() - (HEADER_FIELDS | legacy_fields):
            raise CatalogError("invalid catalog schema")
        custom, overrides, tombstones = state["custom"], state["overrides"], state["tombstones"]
        if not isinstance(custom, dict) or not isinstance(overrides, dict) or not isinstance(tombstones, list):
            raise CatalogError("invalid catalog schema")
        if len(custom) + len(overrides) > MAX_ENTRIES:
            raise CatalogError("catalog entry limit exceeded")
        for sid, fields in custom.items():
            self._validate_id(sid)
            self._validate_fields(fields, V1_FIELDS, required={"title"})
            if sid in entries:
                raise CatalogError("invalid custom source entry")
        for sid, fields in overrides.items():
            self._validate_id(sid)
            self._validate_fields(fields, V1_FIELDS)
            if sid not in entries:
                raise CatalogError("invalid built-in source override")
        deleted = set()
        for sid in tombstones:
            self._validate_id(sid)
            if sid not in entries or sid in deleted:
                raise CatalogError("invalid source tombstones")
            deleted.add(sid)

    @staticmethod
    def _validate_collection(state):
        required = HEADER_FIELDS | {"sources"}
        if (not required <= state.keys() or state.keys() - (required | {"groups", "starter_groups_initialized"})
                or not isinstance(state["sources"], list)
                or ("starter_groups_initialized" in state and state["starter_groups_initialized"] is not True)):
            raise CatalogError("invalid catalog schema")
        if len(state["sources"]) > MAX_ENTRIES:
            raise CatalogError("catalog entry limit exceeded")

    def _validate_v2(self, state):
        self._validate_collection(state)
        ids = set()
        for record in state["sources"]:
            if not isinstance(record, dict) or record.keys() != {"source_id", "profile", "configuration"}:
                raise CatalogError("invalid catalog source")
            sid = record["source_id"]
            self._validate_id(sid)
            if sid in ids:
                raise CatalogError("duplicate source ID")
            ids.add(sid)
            self._validate_fields(record["profile"], PROFILE_FIELDS, required={"title"})
            self._validate_fields(record["configuration"], CONFIG_FIELDS, required={"format"})
        if "groups" in state:
            self._validate_groups(state["groups"], ids)

    def _validated_payload(self, state):
        self._validate_v2(state)
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > MAX_CATALOG_BYTES:
            raise CatalogError("catalog exceeds size limit")
        return payload

    @staticmethod
    def _validate_id(sid):
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
            raise CatalogError("invalid source ID")

    def _validate_fields(self, fields, allowed, *, required: Set[str] = frozenset()):
        if not isinstance(fields, dict) or fields.keys() - allowed or not required <= fields.keys():
            raise CatalogError("invalid source fields")
        for key, value in fields.items():
            self._validate_field(key, value)

    @staticmethod
    def _validate_field(key, value):
        if (not isinstance(value, str) or len(value) > MAX_FIELD
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise CatalogError("invalid source field")
        if key == "title" and not value.strip():
            raise CatalogError("title is required")
        if key == "format" and not ID_RE.fullmatch(value):
            raise CatalogError("invalid source format")
        if key == "expected_size_bytes" and value and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise CatalogError("expected size must be a positive decimal byte count")
        if key == "sha256" and value and not re.fullmatch(r"[a-fA-F0-9]{64}", value):
            raise CatalogError("SHA-256 must contain exactly 64 hexadecimal characters")
        if key == "source_url" and value:
            try:
                parsed = urlsplit(value)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None or parsed.path.startswith("//")):
                    raise ValueError
                if any(part in {".", ".."} for part in parsed.path.split("/")):
                    raise ValueError
                parsed.port
            except ValueError as exc:
                raise CatalogError("source URL must be a safe HTTP(S) URL") from exc

    @staticmethod
    def _snapshot(state):
        return {key: state[key] for key in ("revision", "updated_at", "sources", "groups") if key in state}

    @staticmethod
    def _apply_fields(record, fields):
        for key, value in fields.items():
            record["profile" if key in PROFILE_FIELDS else "configuration"][key] = value

    def _record(self, sid, fields):
        record = {"source_id": sid, "profile": {}, "configuration": {"format": "unspecified"}}
        self._apply_fields(record, fields)
        return record

    def _seed_record(self, entry):
        fields = {key: entry[key] for key in V1_FIELDS & entry.keys()}
        if entry["kind"] != "planned":
            fields["format"] = entry["kind"]
        if "bytes" in entry:
            fields["expected_size_bytes"] = str(entry["bytes"])
        return self._record(entry["source_id"], fields)

    def _convert_v1(self, value, entries):
        custom, overrides, tombstones = value["custom"], value["overrides"], set(value["tombstones"])
        records = []
        for sid, entry in entries.items():
            if sid in tombstones:
                continue
            record = self._seed_record(entry)
            self._apply_fields(record, overrides.get(sid, {}))
            records.append(record)
        records.extend(self._record(sid, custom[sid]) for sid in sorted(custom))
        return {"schema_version": SCHEMA_VERSION, "revision": value["revision"],
                "updated_at": value.get("updated_at"), "sources": records}

    def _convert_draft_v2(self, state):
        # The unfinished implementation wrote this shape during development.
        # Recognize only its exact shape; reject unexpected data instead of
        # silently dropping it. The old marker has no role after importing.
        entries = {entry["source_id"]: entry for entry in source_catalog.builtin_sources()}
        records = []
        for record in state["sources"]:
            if (not isinstance(record, dict)
                    or record.keys() != {"source_id", "profile", "configuration", "candidate", "seeded"}
                    or type(record["seeded"]) is not bool or record["configuration"] != {}):
                raise CatalogError("invalid draft catalog source")
            sid, profile, candidate = record["source_id"], record["profile"], record["candidate"]
            self._validate_id(sid)
            if not isinstance(profile, dict):
                raise CatalogError("invalid draft catalog profile")
            seed = entries.get(sid) if record["seeded"] else None
            if record["seeded"] and seed is None:
                raise CatalogError("unknown draft seed")
            static = {k: v for k, v in (seed or {}).items() if k not in V1_FIELDS | {"source_id"}}
            if (profile.keys() - V1_FIELDS != static.keys()
                    or any(type(profile[k]) is not type(v) or profile[k] != v for k, v in static.items())):
                raise CatalogError("invalid draft catalog metadata")
            fields = {k: v for k, v in profile.items() if k in V1_FIELDS}
            self._validate_fields(fields, V1_FIELDS, required={"title"})
            self._validate_fields(candidate, CANDIDATE_FIELDS)
            if seed is not None:
                defaults = self._seed_record(seed)["configuration"]
                fields = defaults | fields
            records.append(self._record(sid, fields | candidate))
        return dict(state, sources=records)

    def get(self):
        with LOCK:
            fd = self._file_lock()
            try:
                return self._snapshot(self._read(initialize=True))
            finally:
                os.close(fd)

    def mutate(self, operation, source_id, fields, revision, busy=False):
        self._validate_id(source_id)
        if type(revision) is not int or revision < 0:
            raise CatalogError("revision precondition required")
        if busy:
            raise CatalogError("source has active or unresolved acquisition")
        if operation not in ("add", "edit", "remove"):
            raise CatalogError("unknown operation")
        self._validate_fields(fields, EDITABLE, required={"title"} if operation == "add" else frozenset())
        if operation == "remove" and fields:
            raise CatalogError("remove does not accept source fields")
        with LOCK:
            fd = self._file_lock()
            try:
                state = self._read()
                if state["revision"] != revision:
                    raise RevisionConflict("catalog revision changed")
                records = {record["source_id"]: record for record in state["sources"]}
                if operation == "add":
                    if source_id in records:
                        raise CatalogError("source ID already exists")
                    state["sources"].append(self._record(source_id, fields))
                else:
                    if source_id not in records:
                        raise CatalogError("source not found")
                    if operation == "edit":
                        self._apply_fields(records[source_id], fields)
                    else:
                        state["sources"].remove(records[source_id])
                        for group in state.get("groups", []):
                            group["source_ids"] = [sid for sid in group["source_ids"] if sid != source_id]
                state["revision"] += 1
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._atomic_write(self._validated_payload(state))
                return self._snapshot(state)
            finally:
                os.close(fd)


    def _validate_groups(self, groups, source_ids):
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            raise CatalogError("invalid groups or group limit exceeded")
        keys, names = set(), set()
        for group in groups:
            if not isinstance(group, dict) or group.keys() != {"kind", "group_id", "name", "source_ids"}:
                raise CatalogError("invalid group")
            kind, gid, name, members = (group[k] for k in ("kind", "group_id", "name", "source_ids"))
            if not isinstance(kind, str) or kind not in GROUP_KINDS:
                raise CatalogError("invalid group kind")
            self._validate_id(gid)
            self._validate_field("title", name)
            if len(name) > MAX_GROUP_NAME or name != name.strip():
                raise CatalogError("invalid group name")
            if (kind, gid) in keys or (kind, name.casefold()) in names:
                raise CatalogError("duplicate group ID or name")
            keys.add((kind, gid))
            names.add((kind, name.casefold()))
            if not isinstance(members, list) or len(members) > MAX_ENTRIES:
                raise CatalogError("invalid group memberships")
            for sid in members:
                self._validate_id(sid)
            if len(set(members)) != len(members) or not set(members) <= source_ids:
                raise CatalogError("duplicate or unknown source membership")

    def mutate_group(self, operation, kind, group_id, revision, *, name=None, source_ids=None, group_ids=None):
        if not isinstance(kind, str) or kind not in GROUP_KINDS:
            raise CatalogError("invalid group kind")
        self._validate_id(group_id)
        if type(revision) is not int or revision < 0:
            raise CatalogError("revision precondition required")
        bulk = operation in ("bulk-add", "bulk-remove", "bulk-replace")
        if bulk:
            if group_id != "bulk" or name is not None or not isinstance(group_ids, list) or len(group_ids) > MAX_GROUPS:
                raise CatalogError("invalid bulk grouping request")
            for gid in group_ids:
                self._validate_id(gid)
            if len(set(group_ids)) != len(group_ids) or (not group_ids and operation != "bulk-replace"):
                raise CatalogError("invalid bulk grouping IDs")
        elif group_ids is not None:
            raise CatalogError("unexpected grouping IDs")
        if operation not in ("bulk-add", "bulk-remove", "bulk-replace", "create", "rename", "delete", "add-members", "remove-members", "initialize-categories", "merge-defaults"):
            raise CatalogError("unknown group operation")
        if operation in ("create", "rename"):
            self._validate_field("title", name)
            if len(name) > MAX_GROUP_NAME or name != name.strip() or source_ids is not None:
                raise CatalogError("invalid group name or fields")
        elif bulk or operation in ("add-members", "remove-members"):
            if name is not None or not isinstance(source_ids, list) or not 0 < len(source_ids) <= MAX_ENTRIES:
                raise CatalogError("invalid membership request")
            for sid in source_ids:
                self._validate_id(sid)
            if len(set(source_ids)) != len(source_ids):
                raise CatalogError("duplicate source IDs")
        elif name is not None or source_ids is not None:
            raise CatalogError("operation does not accept group fields")
        if operation in ("initialize-categories", "merge-defaults") and (kind != "categories" or group_id != "defaults"):
            raise CatalogError("invalid default category request")
        with LOCK:
            fd = self._file_lock()
            try:
                state = self._read()
                if state["revision"] != revision:
                    raise RevisionConflict("catalog revision changed")
                groups = state.setdefault("groups", [])
                known = {record["source_id"] for record in state["sources"]}
                group = next((g for g in groups if (g["kind"], g["group_id"]) == (kind, group_id)), None)
                if bulk:
                    selected = set(source_ids)
                    targets = set(group_ids)
                    available = {g["group_id"] for g in groups if g["kind"] == kind}
                    if not selected <= known or not targets <= available:
                        raise CatalogError("unknown source or grouping ID")
                    for item in groups:
                        if item["kind"] != kind:
                            continue
                        members = set(item["source_ids"])
                        if item["group_id"] in targets and operation in ("bulk-add", "bulk-replace"):
                            members.update(selected)
                        elif operation == "bulk-replace" or (operation == "bulk-remove" and item["group_id"] in targets):
                            members.difference_update(selected)
                        item["source_ids"] = sorted(members)
                elif operation == "merge-defaults":
                    self._merge_starter_groups(state)
                elif operation == "initialize-categories":
                    if any(g["kind"] == "categories" for g in groups):
                        raise CatalogError("categories already exist; manage them individually")
                    groups.extend(dict(kind="categories", group_id=gid, name=label, source_ids=[])
                                  for gid, label in DEFAULT_CATEGORIES)
                elif operation == "create":
                    if group is not None:
                        raise CatalogError("group ID already exists")
                    groups.append(dict(kind=kind, group_id=group_id, name=name, source_ids=[]))
                else:
                    if group is None:
                        raise CatalogError("group not found")
                    if operation == "rename":
                        group["name"] = name
                    elif operation == "delete":
                        groups.remove(group)
                    else:
                        if not set(source_ids) <= known:
                            raise CatalogError("unknown source ID")
                        members = set(group["source_ids"])
                        if operation == "add-members":
                            members.update(source_ids)
                        else:
                            members.difference_update(source_ids)
                        group["source_ids"] = sorted(members)
                state["revision"] += 1
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._atomic_write(self._validated_payload(state))
                return self._snapshot(state)
            finally:
                os.close(fd)

    def _atomic_write(self, payload):
        fd, name = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
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
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
