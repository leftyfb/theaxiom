#!/usr/bin/env python3
"""Offline library acquisition daemon.

Reads the managed content manifest, downloads missing or stale artifacts,
and writes a status report. Designed to run on a timer via systemd.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "theaxiom"
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "theaxiom"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "theaxiom"
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "theaxiom"
MANIFEST_PATH = Path(os.environ.get("THEAXIOM_MANIFEST", CONFIG_HOME / "acquisition.yaml"))
STATUS_PATH = Path(os.environ.get("THEAXIOM_STATUS_PATH", STATE_HOME / "acquisition-status.json"))
STAGING_DIR = Path(os.environ.get("THEAXIOM_STAGING_DIR", CACHE_HOME / "staging"))
SOURCES_DIR = Path(os.environ.get("THEAXIOM_SOURCES_DIR", DATA_HOME / "sources"))
QUARANTINE_DIR = Path(os.environ.get("THEAXIOM_QUARANTINE_DIR", DATA_HOME / "quarantine"))
DOWNLOAD_TIMEOUT = 3600  # 1 hour per file
MAX_CONCURRENT = 2


def load_manifest() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    if yaml is None:
        print("PyYAML not available", file=sys.stderr)
        return []
    with open(MANIFEST_PATH) as f:
        data = yaml.safe_load(f)
    items = data.get("items", data) if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict) and i.get("enabled")]


def sha256_file(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def download(url: str, dest: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    """Download a file with checksum verification. Returns status dict."""
    result = {"url": url, "dest": str(dest), "status": "pending", "bytes": 0, "error": None}

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Check if already present and valid
    if dest.exists():
        if expected_sha256:
            actual = sha256_file(dest)
            if actual == expected_sha256:
                result["status"] = "present"
                result["bytes"] = dest.stat().st_size
                return result
        else:
            result["status"] = "present"
            result["bytes"] = dest.stat().st_size
            return result

    # Download to staging first
    staging = STAGING_DIR / dest.name
    staging.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "curl", "--fail", "--location", "--retry", "3",
        "--connect-timeout", "30", "--max-time", str(DOWNLOAD_TIMEOUT),
        "--output", str(staging), url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT + 60)
        if proc.returncode != 0:
            result["status"] = "failed"
            result["error"] = f"curl exit {proc.returncode}: {proc.stderr.strip()[:500]}"
            staging.unlink(missing_ok=True)
            return result
    except subprocess.TimeoutExpired:
        result["status"] = "failed"
        result["error"] = "download timed out"
        staging.unlink(missing_ok=True)
        return result

    # Verify checksum if provided
    if expected_sha256:
        actual = sha256_file(staging)
        if actual != expected_sha256:
            result["status"] = "failed"
            result["error"] = f"checksum mismatch: expected {expected_sha256}, got {actual}"
            # Quarantine bad download
            quarantine = QUARANTINE_DIR / dest.name
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(quarantine)
            return result

    # Move to final destination
    staging.rename(dest)
    result["status"] = "downloaded"
    result["bytes"] = dest.stat().st_size
    return result


def run_acquisition() -> dict[str, Any]:
    """Main acquisition pass. Returns status report."""
    items = load_manifest()
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_items": len(items),
        "results": [],
        "errors": [],
    }

    for item in items:
        dest_rel = item.get("destination", "")
        if not dest_rel:
            continue

        dest = SOURCES_DIR / dest_rel
        url = item.get("source_url", "")
        expected_sha = item.get("sha256")

        result = download(url, dest, expected_sha)
        result["id"] = item.get("id", "")
        result["title"] = item.get("title", "")
        report["results"].append(result)

        if result["status"] == "failed":
            report["errors"].append(f"{result['id']}: {result['error']}")

    # Write status
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_PATH, "w") as f:
        json.dump(report, f, indent=2)

    return report


def main() -> int:
    report = run_acquisition()
    ok = sum(1 for r in report["results"] if r["status"] in ("present", "downloaded"))
    failed = sum(1 for r in report["results"] if r["status"] == "failed")
    print(f"Acquisition complete: {ok} ok, {failed} failed, {len(report['results'])} total")
    if report["errors"]:
        for e in report["errors"]:
            print(f"  ERROR: {e}", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
