"""Read-only Linux storage readiness inspection. Not an acquisition safety gate."""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
import time
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


def inspect_storage(settings: dict[str, Any], *, required_bytes: int = 0, progress=None) -> dict[str, Any]:
    """Return advisory readiness without mkdir, file writes, or fallback.

    This cannot guarantee future writes or bound I/O on an unhealthy hard NFS mount.
    The acquisition writer independently validates its opened filesystem anchors.
    """
    result: dict[str, Any] = {"ready": False, "write_ready": False, "root": None,
                              "mount": None, "total_bytes": None, "free_bytes": None,
                              "error": None, "paths": {}}
    operation, failed_path = "validate_settings", None

    def mark(stage, path=None):
        nonlocal operation, failed_path
        operation, failed_path = stage, str(path) if path is not None else None
        if progress is not None:
            progress(operation, failed_path)
    try:
        config = validate_settings(settings)
        if not config["storage_root"]:
            raise ValueError("storage_not_configured")
        root = Path(config["storage_root"])
        result["root"] = str(root)
        mark("inspect_mount", root)
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
            mark("inspect_mount", path)
            if _covering(path, mounts) != mount:
                raise ValueError("nested_or_replaced_mount")
            for parent in reversed((path, *path.parents)):
                if parent == Path("/"):
                    continue
                try:
                    mark("inspect_path", parent)
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
            while True:
                mark("check_access", path)
                try:
                    path.stat()
                    break
                except FileNotFoundError:
                    if path == root:
                        raise ValueError("storage_root_missing")
                    path = path.parent
            if not os.access(path, os.W_OK | os.X_OK, effective_ids=True):
                raise ValueError("storage_not_writable")
        mark("check_capacity", root)
        capacity = os.statvfs(root)
        result["total_bytes"] = capacity.f_blocks * capacity.f_frsize
        result["free_bytes"] = capacity.f_bavail * capacity.f_frsize
        if capacity.f_flag & os.ST_RDONLY:
            raise ValueError("read_only_filesystem")
        mark("inspect_mount", root)
        if _covering(root, read_mounts()) != mount:
            raise ValueError("mount_changed_during_inspection")
        mark("check_capacity", root)
        if result["free_bytes"] < max(config["minimum_free_bytes"], required_bytes):
            raise ValueError("insufficient_capacity")
        result["ready"] = result["write_ready"] = True
    except ValueError as exc:
        result["error"] = str(exc)
    except OSError as exc:
        result["error"] = {
            errno.EACCES: "storage_permission_denied", errno.EPERM: "storage_permission_denied",
            errno.ESTALE: "stale_storage_handle", errno.ETIMEDOUT: "storage_probe_timeout",
            errno.EROFS: "read_only_filesystem", errno.ENOSPC: "insufficient_capacity",
        }.get(exc.errno, "storage_io_error")
    if result["error"]:
        result.update(operation=operation, failed_path=failed_path)
    return result


class StorageProbe:
    """One background probe, even if a hard NFS operation never returns."""

    SAMPLE_MAX_AGE = 3
    STALL_AFTER = 3

    def __init__(self):
        self._lock = threading.Lock()
        self._sample = None
        self._running = None

    def status(self, settings):
        now = time.monotonic()
        if not settings["storage_root"]:
            return inspect_storage(settings)
        with self._lock:
            if self._sample:
                sampled_settings, stamped, result = self._sample
                if sampled_settings == settings and now - stamped < self.SAMPLE_MAX_AGE:
                    return dict(result)
            if self._running is None:
                run = {"settings": settings, "started": now, "operation": "inspect_mount",
                       "failed_path": settings["storage_root"]}
                self._running = run
                try:
                    threading.Thread(target=self._inspect, args=(run,), daemon=True,
                                     name="storage-readiness").start()
                except RuntimeError:
                    self._running = None
                    return dict(ready=False, write_ready=False, error="storage_io_error",
                                root=settings["storage_root"])
            run = self._running
            stalled = now - run["started"] >= self.STALL_AFTER
            result = dict(ready=False, write_ready=False, root=settings["storage_root"],
                          pending=not stalled, error="storage_probe_timeout" if stalled else None)
            if stalled and run["settings"] == settings:
                result.update(operation=run["operation"], failed_path=run["failed_path"])
            return result

    def _inspect(self, run):
        def report(operation, path):
            with self._lock:
                run.update(operation=operation, failed_path=path)

        result = dict(ready=False, write_ready=False, error="storage_io_error",
                      root=run["settings"]["storage_root"])
        try:
            result = inspect_storage(run["settings"], progress=report)
        finally:
            with self._lock:
                self._sample = run["settings"], time.monotonic(), result
                self._running = None
