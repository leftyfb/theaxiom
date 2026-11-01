#!/usr/bin/env python3
"""Test the local-only POC status command."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/theaxiom/poc-status.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(b"status fixture\n")
        report = root / "report.json"
        report.write_text(json.dumps({"service_version": "fixture-poc/1.0", "catalog_result": "passed", "fixture_checksum": "pinned", "transfer_performed": False}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(COMMAND), "--fixture", str(fixture), "--report", str(report), "--service", "nonexistent-vancouver-test.service"], env={**os.environ, "PATH": directory}, capture_output=True, text=True, check=False)
        status = json.loads(result.stdout)
    assert result.returncode == 1
    assert status["service"]["requested"] is True
    assert status["service"]["checked"] is False
    assert status["service"]["active"] is None
    assert status["service"]["state"] == "systemctl unavailable"
    assert status["fixture"]["sha256"]
    assert status["report"]["catalog_result"] == "passed"
    assert status["report"]["transfer_performed"] is False
    print("POC status command: local service, report, and fixture state emitted as JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
