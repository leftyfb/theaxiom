"""Produce portable verification records for explicitly selected local ZIMs."""
import json

from . import library_selection, zim_jobs


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
    manifest = {"version": 1, "kind": "axiom-local-transfer", "artifacts": records}
    if len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > library_selection.MAX_BYTES:
        raise ValueError("Transfer manifest is too large; export fewer sources")
    return manifest
