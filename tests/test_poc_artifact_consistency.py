#!/usr/bin/env python3
"""Test the local POC artifact consistency verifier."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts/poc-artifact-consistency.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(b"offline-library-fixture")
        checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        status = {"service": {"active": True, "state": "active"}, "fixture": {"sha256": checksum}, "report": {"catalog_result": "passed"}}
        catalog = {"sha256": checksum}
        report = {"fixture_checksum": checksum, "catalog_result": "passed", "transfer_performed": False}
        paths = {name: root / f"{name}.json" for name in ("status", "catalog", "report")}
        for name, value in (("status", status), ("catalog", catalog), ("report", report)):
            paths[name].write_text(json.dumps(value), encoding="utf-8")
        result = subprocess.run(
            ["python3", str(VERIFY), "--status", str(paths["status"]), "--catalog", str(paths["catalog"]), "--report", str(paths["report"]), "--fixture", str(fixture)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "agree" in result.stdout
    print("POC artifact consistency test: matching fixture checksum and passing state verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
