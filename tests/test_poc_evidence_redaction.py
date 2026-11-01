#!/usr/bin/env python3
"""Test that POC evidence excludes secret-bearing values and paths."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/theaxiom/poc-evidence-capture.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(b"redaction fixture\n")
        report = root / "report.json"
        report.write_text("{}\n", encoding="utf-8")
        status = root / "status.py"
        status.write_text("#!/usr/bin/env python3\nimport json; print(json.dumps({'environment': 'TOP_SECRET', 'ansible_variable': 'vault-value', 'service': {'active': True}}))\n", encoding="utf-8")
        verify = root / "verify.py"
        verify.write_text("#!/usr/bin/env python3\nprint('TOP_SECRET /etc/ansible/inventory/vault')\n", encoding="utf-8")
        status.chmod(0o755)
        verify.chmod(0o755)
        output = root / "evidence.json"
        env = os.environ.copy()
        env["OFFLINE_LIBRARY_TEST_TOKEN"] = "TOP_SECRET"
        result = subprocess.run(["python3", str(COMMAND), "--fixture", str(fixture), "--report", str(root / "vault/report.json"), "--status-command", str(status), "--verify-command", str(verify), "--output", str(output)], capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, result.stderr
        rendered = output.read_text(encoding="utf-8")
        assert "TOP_SECRET" not in rendered
        assert "vault-value" not in rendered
        assert "/etc/ansible/inventory/vault" not in rendered
        evidence = json.loads(rendered)
        assert evidence["status"]["json"]["environment"] == "[REDACTED]"
        assert evidence["report"]["path"] == "[REDACTED]"
    print("POC evidence redaction test: environment values, secret fields, and sensitive paths redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
