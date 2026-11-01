#!/usr/bin/env python3
"""Capture read-only local POC evidence as one JSON document."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


SENSITIVE_KEY = re.compile(r"(?:secret|token|password|passwd|vault|inventory|ansible|environment|env)", re.IGNORECASE)


def redact(value: object, environment_values: set[str]) -> object:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item, environment_values) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, environment_values) for item in value]
    if isinstance(value, str):
        if re.search(r"(?:/|\\b)(?:vault|inventory|ansible)(?:/|\\b)", value, re.IGNORECASE):
            return "[REDACTED]"
        for secret in environment_values:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


ROOT = Path(__file__).resolve().parents[2]
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "theaxiom"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures/kiwix/tiny.txt")
    parser.add_argument("--report", type=Path, default=ROOT / "fixtures/kiwix/poc-build-update-report.json")
    parser.add_argument("--status-command", required=True, help="command that emits the local status JSON")
    parser.add_argument("--verify-command", required=True, help="command that verifies the local fixture")
    parser.add_argument("--output", type=Path, default=STATE_HOME / "poc-evidence.json")
    args = parser.parse_args()
    status = subprocess.run([args.status_command, "--fixture", str(args.fixture), "--report", str(args.report)], capture_output=True, text=True, check=False)
    verification = subprocess.run([args.verify_command, "--fixture", str(args.fixture), "--report", str(args.report)], capture_output=True, text=True, check=False)
    environment_values = {value for key, value in os.environ.items() if SENSITIVE_KEY.search(key)}
    evidence = {"status": {"returncode": status.returncode, "json": json.loads(status.stdout)}, "verification": {"returncode": verification.returncode, "output": verification.stdout.splitlines()}, "fixture": {"path": str(args.fixture), "sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest()}, "report": {"path": str(args.report)}}
    evidence = redact(evidence, environment_values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0 if status.returncode == 0 and verification.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
