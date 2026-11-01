#!/usr/bin/env python3
"""Validate the local POC release-readiness report."""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/poc-release-readiness.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "readiness.json"
        subprocess.run(["python3", str(GENERATOR), "--output", str(output)], cwd=ROOT, check=True)
        artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["suite"]["result"] == "passed"
    assert artifact["suite"]["report"]["transfer_performed"] is False
    assert artifact["application"]["version"] == "0.1.0"
    assert len(artifact["application"]["commit"]) == 40
    assert len(artifact["fixture_checksum"]) == 64
    assert artifact["bulk_acquisition_enabled"] is False
    print("POC release readiness: suite, application version/commit, fixture checksum, and acquisition gate validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
