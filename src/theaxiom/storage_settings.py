"""Validated, persistent storage settings for The Axiom admin API."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SETTINGS_PATH = Path(os.environ.get("THEAXIOM_STORAGE_SETTINGS", CONFIG_HOME / "theaxiom/storage.json"))
DEFAULT_SETTINGS: dict[str, Any] = {
    "storage_source": "",
    "storage_mount": "/media/offline",
    "minimum_free_bytes": 322122547200,
    "directories": ["sources", "releases", "quarantine", "reports"],
}
MAX_MINIMUM_FREE_BYTES = 2**63 - 1


def validate_settings(value: Any) -> dict[str, Any]:
    """Validate and normalize the complete supported settings object."""
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError("settings must contain exactly the supported fields")
    source = value["storage_source"]
    mount = value["storage_mount"]
    minimum = value["minimum_free_bytes"]
    directories = value["directories"]
    if not isinstance(source, str) or len(source) > 1024 or "\x00" in source:
        raise ValueError("storage_source must be a string of at most 1024 characters")
    if not isinstance(mount, str) or not mount.startswith("/") or "\x00" in mount:
        raise ValueError("storage_mount must be an absolute path")
    mount_path = Path(mount)
    if mount_path == Path("/") or ".." in mount_path.parts:
        raise ValueError("storage_mount cannot be root or contain traversal")
    if type(minimum) is not int or not 0 <= minimum <= MAX_MINIMUM_FREE_BYTES:
        raise ValueError("minimum_free_bytes must be a non-negative bounded integer")
    if not isinstance(directories, list) or not 1 <= len(directories) <= 32:
        raise ValueError("directories must contain between 1 and 32 names")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) or name in {".", ".."} for name in directories):
        raise ValueError("directories must be simple relative names")
    if len(set(directories)) != len(directories):
        raise ValueError("directories must be unique")
    return {
        "storage_source": source,
        "storage_mount": str(mount_path),
        "minimum_free_bytes": minimum,
        "directories": list(directories),
    }


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Load stored settings, falling back to safe defaults when absent."""
    target = path or SETTINGS_PATH
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS, directories=list(DEFAULT_SETTINGS["directories"]))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("stored settings cannot be read") from exc
    return validate_settings(value)


def save_settings(value: Any, path: Path | None = None) -> dict[str, Any]:
    """Validate and atomically persist settings with restrictive permissions."""
    settings = validate_settings(value)
    target = path or SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return settings
