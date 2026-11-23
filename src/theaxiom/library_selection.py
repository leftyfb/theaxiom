"""Portable source choices with read-only matching against a local catalog."""
import json

from . import catalog_store, source_catalog

VERSION = 1
MAX_BYTES = 15000  # Leave room for the admin API request envelope.
FIELDS = {"source_id", "format", "language", "edition"}
FORMATS = {"zim", "pdf", "epub", "unspecified"}
LIGHTWEIGHT_LIMIT = 64 * 1024 * 1024
PRESETS = {
    "emergency": ("Emergency", ("fema-ready", "epa-water", "noaa", "amateur-radio")),
    "medical": ("Medical", ("cdc", "red-cross", "hesperian")),
    "education": ("Education", ("wikibooks", "wikiversity", "openstax", "kolibri")),
    "technical": ("Technical", ("linux-docs", "ifixit", "practical-action", "stack-exchange")),
    "lightweight": ("Lightweight (up to 64 MiB each)", ()),
}


def _validate_entries(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= catalog_store.MAX_ENTRIES:
        raise ValueError("Select at least one source within the catalog limit")
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.keys() != FIELDS
                or any(not isinstance(value, str) or len(value) > catalog_store.MAX_FIELD
                       or any(ord(char) < 32 for char in value) for value in entry.values())
                or not catalog_store.ID_RE.fullmatch(entry["source_id"])
                or entry["format"] not in FORMATS):
            raise ValueError("Invalid selection entry")
        if entry["source_id"] in seen:
            raise ValueError("Selection contains a duplicate source ID")
        seen.add(entry["source_id"])


def _choice(record):
    profile = record["profile"]
    return {"source_id": record["source_id"], "format": record["configuration"]["format"],
            "language": profile.get("language", ""), "edition": profile.get("edition", "")}


def export_selection(known, source_ids):
    if (not isinstance(source_ids, list) or not source_ids
            or any(not isinstance(sid, str) or sid not in known for sid in source_ids)):
        raise ValueError("Unknown selected source")
    sources = [_choice(known[sid]) for sid in source_ids]
    _validate_entries(sources)
    manifest = {"version": VERSION, "sources": sources}
    if len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Selection file is too large; export fewer sources")
    return manifest


def preview_selection(known, manifest):
    if (not isinstance(manifest, dict) or manifest.keys() != {"version", "sources"}
            or type(manifest["version"]) is not int or manifest["version"] != VERSION):
        raise ValueError("Unsupported selection format or version")
    sources = manifest["sources"]
    _validate_entries(sources)
    matches = []
    for entry in sources:
        sid = entry["source_id"]
        local = known.get(sid)
        if not local:
            matches.append(dict(entry, state="missing"))
            continue
        choice = _choice(local)
        row = dict(entry, state="match" if entry == choice else "different",
                   title=local["profile"]["title"], license=local["configuration"].get("license", ""))
        if row["state"] == "different":
            row["local_choice"] = choice
        matches.append(row)
    return matches


def preview_preset(known, preset):
    if not isinstance(preset, str) or preset not in PRESETS:
        raise ValueError("Unknown library preset")
    _, ids = PRESETS[preset]
    if preset == "lightweight":
        ids = []
        for sid, record in known.items():
            config = record["configuration"]
            size = config.get("expected_size_bytes", "")
            if (config["format"] in FORMATS - {"unspecified"} and size
                    and 0 < int(size) <= LIGHTWEIGHT_LIMIT):
                ids.append(sid)
    packaged_titles = {source["source_id"]: source["title"] for source in source_catalog.builtin_sources()}
    rows = []
    for sid in ids:
        record = known.get(sid)
        if record is None:
            rows.append({"source_id": sid, "title": packaged_titles.get(sid, sid), "state": "missing"})
            continue
        choice = _choice(record)
        rows.append(dict(choice, title=record["profile"]["title"],
                         state="match" if choice["format"] in FORMATS - {"unspecified"} else "planned",
                         license=record["configuration"].get("license", "")))
    return rows
