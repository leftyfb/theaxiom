"""Produce portable verification records for explicitly selected local ZIMs."""
import json
import re

from . import catalog_store, library_selection, zim_jobs, zim_source

KIND = "axiom-local-transfer"
VERSION = 1
TEXT_FIELDS = {"source_id", "format", "title", "language", "edition", "filename", "sha256", "license", "provenance"}
FILENAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,249}\.zim")
DIGEST = re.compile(r"[a-f0-9]{64}")


def export_manifest(known, source_ids):
    """Export metadata only, using the verified local installation record."""
    selection = library_selection.export_selection(known, source_ids)
    records = []
    for choice in selection["sources"]:
        sid = choice["source_id"]
        record = known[sid]
        if choice["format"] != "zim":
            raise ValueError("Choose only installed, verified ZIM sources")
        config = record["configuration"]
        try:
            job = zim_jobs.read(sid)
            artifact = (zim_jobs.installed(job, config)
                        if job and job.get("verified") is True and not job.get("active") else None)
        except (OSError, ValueError, KeyError, TypeError):
            artifact = None
        if not artifact:
            raise ValueError("Verified installation unavailable for " + sid)
        records.append(dict(choice, title=record["profile"]["title"],
                            filename=artifact["filename"], bytes=artifact["bytes"],
                            sha256=artifact["sha256"], license=config.get("license", ""),
                            provenance=config.get("provenance", "")))
    manifest = {"version": VERSION, "kind": KIND, "artifacts": records}
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.keys() != {"kind", "version", "artifacts"}
            or manifest["kind"] != KIND or type(manifest["version"]) is not int or manifest["version"] != VERSION):
        raise ValueError("Unsupported transfer manifest")
    records = manifest["artifacts"]
    if not isinstance(records, list) or not 1 <= len(records) <= catalog_store.MAX_ENTRIES:
        raise ValueError("Select a transfer manifest with at least one artifact")
    seen_ids, seen_names = set(), set()
    for artifact in records:
        if (not isinstance(artifact, dict) or artifact.keys() != TEXT_FIELDS | {"bytes"}
                or any(not isinstance(artifact[key], str) or len(artifact[key]) > catalog_store.MAX_FIELD
                       or any(ord(char) < 32 for char in artifact[key]) for key in TEXT_FIELDS)
                or artifact["format"] != "zim" or not catalog_store.ID_RE.fullmatch(artifact["source_id"])
                or not artifact["title"].strip() or not DIGEST.fullmatch(artifact["sha256"])
                or not FILENAME.fullmatch(artifact["filename"]) or ".." in artifact["filename"]
                or type(artifact["bytes"]) is not int or not 0 < artifact["bytes"] <= zim_source.MAX_BYTES):
            raise ValueError("Invalid transfer artifact metadata")
        sid, filename = artifact["source_id"], artifact["filename"]
        if sid in seen_ids or filename in seen_names:
            raise ValueError("Transfer manifest contains duplicate sources or filenames")
        seen_ids.add(sid)
        seen_names.add(filename)
    if len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > library_selection.MAX_BYTES:
        raise ValueError("Transfer manifest is too large")
    return records


def preview_manifest(known, manifest):
    """Compare imported metadata with local records without opening any archives."""
    result = []
    for artifact in _validate_manifest(manifest):
        sid = artifact["source_id"]
        state = "new_source"
        if sid in known:
            state = "source_exists"
            try:
                job = zim_jobs.read(sid) if known[sid]["configuration"]["format"] == "zim" else None
            except (OSError, ValueError, KeyError, TypeError):
                job = None
            if job and job["state"] == "completed" and job.get("verified") is True and not job.get("active"):
                current = job.get("artifact")
                if isinstance(current, dict):
                    state = ("matching_record" if current.get("sha256") == artifact["sha256"]
                             and current.get("bytes") == artifact["bytes"] else "conflicting_record")
        result.append(dict(artifact, state=state))
    return result
