#!/usr/bin/env python3
"""Validate the deterministic offline-library POC release manifest."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/poc-release-manifest.py"
EXPECTED = {
    "fixtures/kiwix/tiny.txt",
    "templates/kiwix-homepage.html.j2",
    "templates/poc-catalog.json.j2",
    "schemas/poc-build-update-report.schema.json",
}


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "manifest.json"
        subprocess.run(["python3", str(GENERATOR), "--output", str(output)], cwd=ROOT, check=True, capture_output=True, text=True)
        manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["format"].endswith("/v1")
    artifacts = manifest["artifacts"]
    assert {item["path"] for item in artifacts} == EXPECTED
    assert len(artifacts) == len(EXPECTED)
    assert all(len(item["sha256"]) == 64 for item in artifacts)
    print("POC release manifest: fixture and release artifact checksums validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
