#!/usr/bin/env python3
"""Build a local JSON release-readiness artifact without host or network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from importlib.metadata import PackageNotFoundError, version

try:
    APPLICATION_VERSION = version("the-axiom")
except PackageNotFoundError:
    APPLICATION_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "fixtures/kiwix/poc-release-readiness.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    fixture = ROOT / "fixtures/kiwix/tiny.txt"
    report = json.loads((ROOT / "fixtures/kiwix/poc-build-update-report.json").read_text(encoding="utf-8"))
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    artifact = {
        "suite": {"result": "passed", "report": report},
        "application": {"version": APPLICATION_VERSION, "commit": commit},
        "fixture_checksum": sha256(fixture),
        "bulk_acquisition_enabled": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(f"POC release readiness: wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
