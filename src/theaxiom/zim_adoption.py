"""Register the verified legacy Wikipedia archive without modifying its filesystem entry."""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from . import acquisition_monitor, pdf_source, zim_jobs

SOURCE_ID = "wikipedia-en"
PINNED_FILENAME = "wikipedia_en_all_maxi_2026-08.zim"
PINNED_SOURCE_URL = "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"
PINNED_BYTES = 127418087648
PINNED_SHA256 = "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6"
SAFE_MODE_MASK = 0o022
MARKER_MAX_BYTES = 16 * 1024


class AdoptionError(ValueError):
    """An adoption precondition or durable transition is unsafe."""


def _json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise AdoptionError("adoption journal is missing")
    except OSError as exc:
        raise AdoptionError("adoption journal must be a regular file") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & SAFE_MODE_MASK):
            raise AdoptionError("adoption journal is not a private regular file")
        raw = os.read(fd, MARKER_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MARKER_MAX_BYTES:
        raise AdoptionError("adoption journal is too large")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AdoptionError("adoption journal is invalid") from exc
    if not isinstance(value, dict):
        raise AdoptionError("adoption journal is invalid")
    return value


def journal_path() -> Path:
    return zim_jobs.directory() / f"{SOURCE_ID}.adoption.json"


def managed() -> bool:
    """Keep the source managed after subsequent ordinary ZIM jobs."""
    try:
        record = _read_json(journal_path())
        job = record["job"]
        if (job["source_id"] != SOURCE_ID
                or job["artifact"]["sha256"] != PINNED_SHA256
                or job["artifact"]["filename"] != PINNED_FILENAME):
            return False
        if record["state"] == "completed":
            return True
        return (record["state"] in {"prepared"}
                and zim_jobs.read(SOURCE_ID) == job)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def adopt(legacy_path: str | Path, config: dict, *, evidence_unit: str | None = None) -> dict:

    legacy = Path(legacy_path)
    journal = journal_path()
    settings = zim_jobs.storage_settings.load_settings()
    if (not settings["storage_root"] or not settings["storage_mount"]
            or legacy != Path(settings["storage_root"]) / "library" / "zim" / PINNED_FILENAME):
        raise AdoptionError("archive must be the pinned filename on the configured mount")
    configuration = dict(format="zim", source_url=PINNED_SOURCE_URL,
                         expected_size_bytes=str(PINNED_BYTES), sha256=PINNED_SHA256)
    if zim_jobs.identity(config) != configuration:
        raise AdoptionError("saved source must match the installed archive pin")
    artifact = dict(filename=PINNED_FILENAME, source_url=PINNED_SOURCE_URL,
                    bytes=PINNED_BYTES, sha256=PINNED_SHA256)
    with zim_jobs.LOCK, zim_jobs.worker_lock():
        current = zim_jobs.read(SOURCE_ID)
        if zim_jobs.read_candidate(SOURCE_ID) is not None:
            raise AdoptionError("a replacement candidate exists")
        record = _read_json(journal) if journal.exists() else None
        if record and (record.get("state") not in {"prepared", "completed"}
                       or record.get("job", {}).get("artifact") != artifact
                       or record["job"].get("configuration") != configuration):
            raise AdoptionError("adoption journal conflicts with this archive")
        if current is not None and (not record or record["job"] != current):
            raise AdoptionError("an unrelated managed job already exists")
        with pdf_source._directory("library", format_name="zim") as (parent, mount, _):
            with pdf_source._file(parent, mount, PINNED_FILENAME, PINNED_BYTES) as stream:
                info = os.fstat(stream.fileno())
                identity = zim_jobs.archive_identity(info)
                if record is None:
                    if not acquisition_monitor.valid_unit_name(evidence_unit):
                        raise AdoptionError("An explicit evidence unit is required for a new adoption")
                    unit = acquisition_monitor.unit_state(evidence_unit)
                    evidence = acquisition_monitor.pinned_invocation_evidence(
                        artifact | {"source_id": SOURCE_ID}, str(legacy), info, unit, evidence_unit)
                    if evidence is None:
                        raise AdoptionError("current acquisition evidence is unavailable")
                    job = dict(source_id=SOURCE_ID, state="completed", active=False,
                               verified=True, configuration=configuration, artifact=artifact,
                               adopted_from_legacy={"identity": identity, "path": str(legacy),
                                                    "evidence": evidence, "adopted_at": time.time()})
                    record = {"state": "prepared", "job": job}
                    _json_atomic(journal, record)
                job = record["job"]
                if (job.get("adopted_from_legacy", {}).get("identity") != identity
                        or zim_jobs.archive_identity(os.stat(
                            PINNED_FILENAME, dir_fd=parent, follow_symlinks=False)) != identity):
                    raise AdoptionError("archive identity changed")
                if current is None:
                    if record["state"] == "completed":
                        raise AdoptionError("completed adoption ledger is missing")
                    zim_jobs.save(job)
                record["state"] = "completed"
                _json_atomic(journal, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("adopt")
    a.add_argument("legacy_path")
    a.add_argument("--evidence-unit")
    args = parser.parse_args()
    try:
        from . import admin_api, publication

        record = next((item for item in admin_api.SOURCE_CATALOG.get()["sources"]
                       if item["source_id"] == SOURCE_ID), None)
        if record is None:
            raise AdoptionError("saved Wikipedia source is missing")
        config = record["configuration"]
        if publication.load().get(SOURCE_ID) and not config.get("license", "").strip():
            raise AdoptionError("published source has no saved license")
        result = adopt(args.legacy_path, config, evidence_unit=args.evidence_unit)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(result["state"])


if __name__ == "__main__":
    main()
