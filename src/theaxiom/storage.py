"""Read-only Linux storage readiness inspection. Not an acquisition safety gate."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from .storage_settings import validate_settings


def _decode(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def read_mounts() -> list[dict[str, Any]]:
    """Read the current process mount namespace without accessing mount contents."""
    rows = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        rows.append({"id": int(left[0]), "target": _decode(left[4]), "source": _decode(right[1]),
                     "type": right[0], "device": left[2],
                     "read_only": "ro" in left[5].split(",") or "ro" in right[2].split(",")})
    return rows


def _covering(path: Path, mounts: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [m for m in mounts if path.is_relative_to(Path(m["target"]))]
    if not matches:
        raise ValueError("no covering mount")
    depth = max(len(Path(m["target"]).parts) for m in matches)
    deepest = [m for m in matches if len(Path(m["target"]).parts) == depth]
    if len(deepest) != 1:
        raise ValueError("ambiguous covering mount")
    return deepest[0]


def inspect_storage(settings: dict[str, Any], *, required_bytes: int = 0) -> dict[str, Any]:
    """Return advisory readiness without mkdir, file writes, or fallback.

    This cannot guarantee future writes or bound I/O on an unhealthy hard NFS mount.
    Acquisition must remain disabled until a separately implemented anchored writer exists.
    """
    result: dict[str, Any] = {"ready": False, "write_ready": False, "root": None,
                              "mount": None, "total_bytes": None, "free_bytes": None,
                              "error": None, "paths": {}}
    try:
        config = validate_settings(settings)
        if not config["storage_root"]:
            raise ValueError("storage_not_configured")
        root = Path(config["storage_root"])
        result["root"] = str(root)
        mounts = read_mounts()
        mount = _covering(root, mounts)
        result["mount"] = mount
        if config["storage_mount"] and (
            mount["target"] != config["storage_mount"] or mount["source"] != config["storage_source"]
        ):
            raise ValueError("required_mount_missing_or_replaced")
        if mount["read_only"]:
            raise ValueError("read_only_filesystem")
        paths = {"staging": root / "staging", "library": root / "library" / "zim"}
        result["paths"] = {key: str(path) for key, path in paths.items()}
        # Reject symlinks and nested mounts throughout managed paths before any capacity probe.
        for path in (root, *paths.values()):
            if _covering(path, mounts) != mount:
                raise ValueError("nested_or_replaced_mount")
            for parent in reversed((path, *path.parents)):
                if parent == Path("/"):
                    continue
                try:
                    mode = parent.lstat().st_mode
                except FileNotFoundError:
                    if parent == root or root.is_relative_to(parent):
                        raise ValueError("storage_root_missing")
                    break
                if stat.S_ISLNK(mode):
                    raise ValueError("symlink_not_allowed")
                if not stat.S_ISDIR(mode):
                    raise ValueError("storage_path_not_directory")
        for path in (root, *paths.values()):
            while not path.exists():
                path = path.parent
            if not os.access(path, os.W_OK | os.X_OK, effective_ids=True):
                raise ValueError("storage_not_writable")
        capacity = os.statvfs(root)
        result["total_bytes"] = capacity.f_blocks * capacity.f_frsize
        result["free_bytes"] = capacity.f_bavail * capacity.f_frsize
        if capacity.f_flag & os.ST_RDONLY:
            raise ValueError("read_only_filesystem")
        if _covering(root, read_mounts()) != mount:
            raise ValueError("mount_changed_during_inspection")
        if result["free_bytes"] < max(config["minimum_free_bytes"], required_bytes):
            raise ValueError("insufficient_capacity")
        result["ready"] = result["write_ready"] = True
    except ValueError as exc:
        result["error"] = str(exc)
    except OSError:
        result["error"] = "storage_io_error"
    return result
