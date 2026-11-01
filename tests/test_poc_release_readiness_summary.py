#!/usr/bin/env python3
"""Validate concise rendering of the release-readiness artifact."""

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "src/theaxiom/poc-build-update-summary.py"
REPORT = ROOT / "fixtures/kiwix/poc-release-readiness.json"


def main() -> int:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "readiness.json"
        source.write_text(json.dumps(report), encoding="utf-8")
        result = subprocess.run(
            ["python3", str(RENDERER), str(source)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    output = result.stdout
    readiness = report["suite"]["report"]
    assert f"POC fixture: {readiness['fixture_checksum']}" in output
    assert f"Service: {readiness['service_version']}" in output
    assert "Catalog: passed" in output
    assert "HTTP fixture: 200" in output
    assert "Transfer performed: no" in output
    assert "http://" not in output and "https://" not in output
    print("POC release-readiness summary: deterministic human-readable rendering validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
