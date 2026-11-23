"""Fail-closed adoption of an existing, operator-verified legacy ZIM.

This module only moves an already verified inode and records ordinary completed
ZIM-job metadata. It never downloads, hashes, changes mounts, or publishes.
The caller owns deployment/publication orchestration and must hold its locks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from . import acquisition_monitor, zim_jobs

SOURCE_ID = "wikipedia-en"
PINNED_FILENAME = "wikipedia_en_all_maxi_2026-08.zim"
PINNED_SOURCE_URL = "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"
PINNED_BYTES = 127418087648
PINNED_SHA256 = "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6"
SAFE_MODE_MASK = 0o022
MARKER_MAX_BYTES = 16 * 1024
# The legacy acquisition account observed during the cutover preflight.  Test
# fixtures deliberately replace the pinned identity, so they retain the local
# test account rather than being forced to impersonate the production account.
EXPECTED_ARCHIVE_OWNER = (1030, 983)


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


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or path.name != PINNED_FILENAME:
        raise AdoptionError("legacy archive must be the exact pinned filename")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            if part in {"", ".", ".."}:
                raise AdoptionError("unsafe legacy archive path")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _identity(fd: int, *, allowed_links: tuple[int, ...] = (1,)) -> dict[str, int]:
    info = os.fstat(fd)
    expected_owner = (EXPECTED_ARCHIVE_OWNER
                      if (PINNED_FILENAME == "wikipedia_en_all_maxi_2026-08.zim"
                          and PINNED_BYTES == 127418087648
                          and PINNED_SHA256 == "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6")
                      else (os.geteuid(), os.getegid()))
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in allowed_links
            or (info.st_uid, info.st_gid) != expected_owner
            or info.st_mode & SAFE_MODE_MASK
            or info.st_size != PINNED_BYTES):
        raise AdoptionError("legacy archive is not an exclusive safe pinned regular file")
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _same_identity(fd: int, expected: dict[str, int], *, allowed_links: tuple[int, ...] = (1, 2)) -> bool:
    try:
        current = _identity(fd, allowed_links=allowed_links)
    except (OSError, AdoptionError):
        return False
    # Linking/unlinking changes ctime and link count, but must not change the
    # anchored inode, length, or mtime.  mtime is part of the evidence: a
    # replacement inode with the same size is never an acceptable recovery.
    return all(current.get(key) == expected.get(key)
               for key in ("device", "inode", "bytes", "mtime_ns"))


def _destination_name() -> str:
    return f"{SOURCE_ID}.{PINNED_SHA256}.zim"


def _require_private_parent(parent_fd: int) -> None:
    """Refuse mutation through a group/world-writable archive directory.

    The anchored descriptor prevents path substitution, while this check avoids
    granting an insecure parent directory a mutation window.  ACLs that grant
    additional write access are not safely inferable from ``stat`` alone, so
    callers must fail closed rather than trying to repair the directory.
    """
    info = os.fstat(parent_fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & SAFE_MODE_MASK:
        raise AdoptionError("legacy archive directory has unsafe write permissions")


def _move_noreplace(parent_fd: int, source: str, destination: str) -> None:
    """Move without overwrite; refuse platforms lacking a safe primitive.

    A hard-link plus unlink is atomic with respect to destination existence and
    does not perform the unsafe check-then-overwrite behavior of os.rename.
    """
    _require_private_parent(parent_fd)
    try:
        os.link(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
    except (FileExistsError, OSError) as exc:
        if isinstance(exc, FileExistsError):
            raise AdoptionError("managed destination already exists") from exc
        raise AdoptionError("safe no-overwrite move is unavailable") from exc
    try:
        os.fsync(parent_fd)
        os.unlink(source, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException as exc:
        raise AdoptionError("safe no-overwrite move did not complete") from exc


def _ledger_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("format") != "zim":
        raise AdoptionError("adoption requires a ZIM configuration")
    if config.get("source_url") != PINNED_SOURCE_URL:
        raise AdoptionError("adoption requires the exact pinned source URL")
    result = {"format": "zim", "source_url": config["source_url"],
              "expected_size_bytes": str(PINNED_BYTES), "sha256": PINNED_SHA256}
    return result


def adopt(legacy_path: str | Path, config: dict[str, Any], journal_path: str | Path,
          *, now: float | None = None) -> dict[str, Any]:
    """Adopt the existing inode into the ordinary completed ZIM ledger.

    Idempotent only when the durable journal and destination prove the same
    transition. Any ambiguity raises without moving or overwriting anything.
    """
    legacy = Path(legacy_path)
    journal = Path(journal_path)
    configuration = _ledger_config(config)
    settings = zim_jobs.storage_settings.load_settings()
    expected_parent = Path(settings["storage_root"]) / "library" / "zim" if settings["storage_root"] else None
    if not legacy.is_absolute() or expected_parent is None or legacy.parent != expected_parent:
        raise AdoptionError("legacy archive must be in the configured library/zim directory")
    existing = _read_json(journal) if journal.exists() else None
    if existing and existing.get("state") == "completed":
        if existing.get("configuration") != configuration:
            raise AdoptionError("adoption journal conflicts with requested configuration")
        if existing.get("identity", {}).get("bytes") != PINNED_BYTES:
            raise AdoptionError("adoption journal conflicts with current inode")
        parent_fd, _ = _open_parent(legacy)
        try:
            destination = existing.get("destination")
            if not isinstance(destination, str):
                raise AdoptionError("adoption journal has no destination")
            fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            try:
                if not _same_identity(fd, existing.get("identity", {})):
                    raise AdoptionError("adoption journal conflicts with current inode")
            finally:
                os.close(fd)
            return existing
        finally:
            os.close(parent_fd)
    parent_fd, name = _open_parent(legacy)
    try:
        _require_private_parent(parent_fd)
        # A prepared journal can survive any point in the link/unlink move:
        # before linking (legacy only), after linking (both names), or after
        # unlinking (managed only).  Recover only the exact anchored inode.
        if existing and existing.get("state") == "prepared":
            destination = existing.get("destination")
            anchored = existing.get("identity")
            if not isinstance(destination, str) or not isinstance(anchored, dict):
                raise AdoptionError("prepared adoption journal is incomplete")
            source_fd = None
            dest_fd = None
            try:
                try:
                    source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                        dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                try:
                    dest_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                      dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                if source_fd is not None and dest_fd is not None:
                    if (not _same_identity(source_fd, anchored, allowed_links=(1, 2))
                            or not _same_identity(dest_fd, anchored, allowed_links=(1, 2))):
                        raise AdoptionError("prepared adoption inode changed")
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                elif dest_fd is not None:
                    if not _same_identity(dest_fd, anchored, allowed_links=(1, 2)):
                        raise AdoptionError("prepared adoption inode changed")
                elif source_fd is not None:
                    if not _same_identity(source_fd, anchored, allowed_links=(1,)):
                        raise AdoptionError("prepared adoption inode changed")
                    _move_noreplace(parent_fd, name, destination)
                else:
                    raise AdoptionError("prepared adoption has neither archive link")
            finally:
                if source_fd is not None:
                    os.close(source_fd)
                if dest_fd is not None:
                    os.close(dest_fd)
            existing["state"] = "moved"
            _json_atomic(journal, existing)

        # Crash recovery after the no-overwrite move but before ledger save.
        if existing and existing.get("state") == "moved":
            destination = existing.get("destination")
            if not isinstance(destination, str):
                raise AdoptionError("adoption journal has no destination")
            try:
                dest_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise AdoptionError("moved adoption destination is missing") from exc
            try:
                if not _same_identity(dest_fd, existing.get("identity", {})):
                    raise AdoptionError("moved adoption inode changed")
            finally:
                os.close(dest_fd)
            if zim_jobs.read(SOURCE_ID) is not None:
                raise AdoptionError("ordinary Wikipedia job diverged during recovery")
            job = {"source_id": SOURCE_ID, "state": "completed", "active": False,
                   "verified": True, "configuration": configuration,
                   "artifact": {"filename": destination, "source_url": configuration["source_url"],
                                "bytes": PINNED_BYTES, "sha256": PINNED_SHA256},
                   "adopted_from_legacy": {"path": str(legacy), "evidence": existing["evidence"],
                                           "identity": existing["identity"], "adopted_at": time.time()}}
            zim_jobs.save(job)
            existing.update(state="completed", job=job)
            _json_atomic(journal, existing)
            return existing
        source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            identity = _identity(source_fd)
            before_stat = os.fstat(source_fd)
            if existing:
                evidence = existing.get("evidence")
                if evidence is None:
                    raise AdoptionError("adoption journal has no persisted evidence")
            else:
                unit = acquisition_monitor.unit_state()
                evidence = acquisition_monitor.pinned_invocation_evidence(
                    {"source_id": SOURCE_ID, "bytes": PINNED_BYTES, "sha256": PINNED_SHA256},
                    str(legacy), before_stat, unit)
                if evidence is None:
                    raise AdoptionError("current acquisition evidence is unavailable")
            after_stat = os.fstat(source_fd)
            if any(getattr(before_stat, field) != getattr(after_stat, field)
                   for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
                raise AdoptionError("legacy archive changed while collecting evidence")
            destination = _destination_name()
            if existing:
                if existing.get("state") == "completed" and existing.get("identity") == identity:
                    return existing
                if existing.get("identity") != identity or existing.get("destination") != destination:
                    raise AdoptionError("adoption journal conflicts with current inode")
            live = zim_jobs.read(SOURCE_ID)
            if live and live.get("state") == "completed":
                raise AdoptionError("ordinary Wikipedia job already exists; refusing overwrite")
            record = {"source_id": SOURCE_ID, "state": "prepared", "active": False,
                      "verified": False, "configuration": configuration,
                      "legacy_path": str(legacy), "destination": destination,
                      "identity": identity, "evidence": evidence,
                      "created_at": now if now is not None else time.time()}
            _json_atomic(journal, record)
            # Revalidate immediately before the only filesystem mutation.
            if not _same_identity(source_fd, identity):
                raise AdoptionError("legacy inode changed before adoption")
            try:
                os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AdoptionError("managed destination already exists")
            _move_noreplace(parent_fd, name, destination)
            record["state"] = "moved"
            _json_atomic(journal, record)
            dest_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            try:
                if not _same_identity(dest_fd, identity):
                    raise AdoptionError("managed destination inode changed after move")
            finally:
                os.close(dest_fd)
            job = {"source_id": SOURCE_ID, "state": "completed", "active": False,
                   "verified": True, "configuration": configuration,
                   "artifact": {"filename": destination, "source_url": configuration["source_url"],
                                "bytes": PINNED_BYTES, "sha256": PINNED_SHA256},
                   "adopted_from_legacy": {"path": str(legacy), "evidence": evidence,
                                           "identity": identity, "adopted_at": time.time()}}
            zim_jobs.save(job)
            record.update(state="completed", job=job)
            _json_atomic(journal, record)
            return record
        finally:
            os.close(source_fd)
    finally:
        os.close(parent_fd)


def rollback(journal_path: str | Path) -> dict[str, Any]:
    """Restore the original filename and remove only the adopted ledger record."""
    journal = Path(journal_path)
    record = _read_json(journal)
    if record.get("state") not in {"moved", "completed", "rolling_back"}:
        raise AdoptionError("adoption is not in a rollback-ready state")
    legacy = Path(record.get("legacy_path", ""))
    if legacy.name != PINNED_FILENAME or not legacy.is_absolute():
        raise AdoptionError("journal has an unsafe legacy path")
    parent_fd, name = _open_parent(legacy)
    try:
        _require_private_parent(parent_fd)
        destination = record.get("destination")
        identity = record.get("identity", {})
        if not isinstance(destination, str) or not re.fullmatch(r"wikipedia-en\.[a-f0-9]{64}\.zim", destination):
            raise AdoptionError("journal has an unsafe destination")

        def opened(path: str):
            try:
                return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=parent_fd)
            except FileNotFoundError:
                return None

        dest_fd = opened(destination)
        legacy_fd = opened(name)
        try:
            if dest_fd is not None and not _same_identity(dest_fd, identity, allowed_links=(1, 2)):
                raise AdoptionError("adopted inode changed; rollback refused")
            if legacy_fd is not None and not _same_identity(legacy_fd, identity, allowed_links=(1, 2)):
                raise AdoptionError("legacy inode changed; rollback refused")
            if dest_fd is None and legacy_fd is None:
                raise AdoptionError("rollback archive is missing")

            # Recovery state is durable before the first movement.  Initial
            # rollback still requires the ordinary ledger to be exact; a
            # retry may tolerate its already-deleted window.
            if record["state"] != "rolling_back":
                if dest_fd is None or legacy_fd is not None:
                    raise AdoptionError("rollback links are in an unsafe state")
                job = zim_jobs.read(SOURCE_ID)
                if (not job or job.get("state") != "completed"
                        or job.get("artifact", {}).get("filename") != destination):
                    raise AdoptionError("ordinary adopted job is missing or diverged")
                record["state"] = "rolling_back"
                _json_atomic(journal, record)
            if dest_fd is not None and legacy_fd is not None:
                os.unlink(destination, dir_fd=parent_fd)
                os.fsync(parent_fd)
            elif dest_fd is not None:
                _move_noreplace(parent_fd, destination, name)
        finally:
            if dest_fd is not None:
                os.close(dest_fd)
            if legacy_fd is not None:
                os.close(legacy_fd)

        job = zim_jobs.read(SOURCE_ID)
        if job is not None:
            if (job.get("state") != "completed"
                    or job.get("artifact", {}).get("filename") != destination):
                raise AdoptionError("ordinary adopted job diverged during rollback")
            target = zim_jobs.path_for(SOURCE_ID)
            if target.is_symlink():
                raise AdoptionError("ordinary job record is unsafe")
            target.unlink()
            dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        record["state"] = "rolled_back"
        _json_atomic(journal, record)
        return record
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adopt or roll back a verified legacy Wikipedia ZIM")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("adopt")
    a.add_argument("legacy_path"); a.add_argument("journal_path"); a.add_argument("source_url")
    r = sub.add_parser("rollback"); r.add_argument("journal_path")
    args = parser.parse_args(argv)
    try:
        if args.command == "adopt":
            result = adopt(args.legacy_path,
                           {"format": "zim", "source_url": args.source_url}, args.journal_path)
        else:
            result = rollback(args.journal_path)
    except (AdoptionError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
