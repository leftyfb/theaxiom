#!/usr/bin/env python3
"""Test the read-only POC evidence capture command."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/theaxiom/poc-evidence-capture.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(b"evidence fixture\n")
        checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        report = root / "report.json"
        report.write_text(json.dumps({"fixture_checksum": checksum, "catalog_result": "passed"}), encoding="utf-8")
        status = root / "status.py"
        status.write_text("#!/usr/bin/env python3\nimport json; print(json.dumps({'service': {'active': True}}))\n", encoding="utf-8")
        verify = root / "verify.py"
        verify.write_text("#!/usr/bin/env python3\nprint('fixture_status: passed')\n", encoding="utf-8")
        status.chmod(0o755)
        verify.chmod(0o755)
        output = root / "evidence.json"
        result = subprocess.run(["python3", str(COMMAND), "--fixture", str(fixture), "--report", str(report), "--status-command", str(status), "--verify-command", str(verify), "--output", str(output)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        evidence = json.loads(output.read_text(encoding="utf-8"))
        assert evidence["fixture"]["sha256"] == checksum
        assert evidence["report"]["path"] == str(report)
        assert evidence["verification"]["output"] == ["fixture_status: passed"]
    print("POC evidence capture test: status, verification, checksum, and report path recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
