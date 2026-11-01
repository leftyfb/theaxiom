#!/usr/bin/env python3
"""Test local POC evidence validation and checksum disagreement handling."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "scripts/poc-evidence-validation.py"


def run(evidence: dict, report: dict, root: Path) -> subprocess.CompletedProcess[str]:
    evidence_path = root / "evidence.json"
    report_path = root / "report.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        ["python3", str(VALIDATE), "--evidence", str(evidence_path), "--report", str(report_path)],
        capture_output=True, text=True, check=False,
    )


def main() -> int:
    checksum = hashlib.sha256(b"offline-library-fixture").hexdigest()
    evidence = {
        "fixture": {"path": "fixture", "sha256": checksum},
        "report": {"path": "report.json"},
        "status": {"returncode": 0, "json": {"fixture": {"sha256": checksum}}},
        "verification": {"returncode": 0, "output": ["fixture_status: passed"]},
    }
    report = {"fixture_checksum": checksum, "catalog_result": "passed"}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = run(evidence, report, root)
        assert result.returncode == 0, result.stdout + result.stderr
        report["fixture_checksum"] = "0" * 64
        result = run(evidence, report, root)
        assert result.returncode != 0
        assert "disagrees" in result.stdout
        evidence["verification"]["returncode"] = 1
        result = run(evidence, report, root)
        assert result.returncode != 0
        assert "verification returned nonzero" in result.stdout
    print("POC evidence validation test: passing evidence accepted and nonzero/checksum disagreement rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
