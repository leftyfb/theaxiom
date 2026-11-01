#!/usr/bin/env python3
"""Build a local, checksum-pinned manifest for the offline-library POC."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "fixtures/kiwix/poc-release-manifest.json"
ARTIFACTS = (
    "fixtures/kiwix/tiny.txt",
    "templates/kiwix-homepage.html.j2",
    "templates/poc-catalog.json.j2",
    "schemas/poc-build-update-report.schema.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = {
        "format": "vancouver-offline-library-poc-release-manifest/v1",
        "artifacts": [
            {"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}
            for path in ARTIFACTS
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"POC release manifest: wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
