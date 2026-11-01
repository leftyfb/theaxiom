#!/usr/bin/env python3
"""Verify POC status emits valid JSON and reflects health in its exit code."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "src/theaxiom/poc-status.py"


def run_status(report, service_output="active", service_code=0):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(b"fixture")
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        systemctl = root / "systemctl"
        systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' '{service_output}'\nexit {service_code}\n", encoding="utf-8")
        systemctl.chmod(0o755)
        result = subprocess.run(
            ["python3", str(STATUS), "--fixture", str(fixture), "--report", str(report_path), "--service", "test.service"],
            env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
        )
        return result, json.loads(result.stdout)


def main() -> int:
    checksum = hashlib.sha256(b"fixture").hexdigest()
    report = {"service_version": "fixture-poc/1.0", "catalog_result": "passed", "fixture_checksum": checksum, "transfer_performed": False}

    healthy, healthy_json = run_status(report)
    assert healthy.returncode == 0
    assert healthy_json["service"]["active"] is True

    for unhealthy_report, output, code in (
        ({**report, "fixture_checksum": "0" * 64}, "active", 0),
        ({**report, "catalog_result": "failed"}, "active", 0),
        (report, "inactive", 3),
    ):
        result, status = run_status(unhealthy_report, output, code)
        assert result.returncode == 1
        assert status["fixture"]["sha256"] == checksum

    print("POC status health exit: healthy JSON exits 0 and unhealthy service, checksum, and report states exit 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
