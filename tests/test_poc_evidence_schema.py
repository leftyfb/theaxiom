#!/usr/bin/env python3
"""Validate the machine-readable POC evidence schema and fixture."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/poc-evidence.schema.json"
EVIDENCE = ROOT / "fixtures/kiwix/poc-evidence.json"


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    required = {"fixture", "report", "status", "verification"}
    assert set(schema["required"]) == required
    assert set(evidence) == required
    assert set(schema["properties"]["fixture"]["required"]) == {"path", "sha256"}
    assert re.fullmatch(r"[a-f0-9]{64}", evidence["fixture"]["sha256"])
    assert evidence["status"]["returncode"] == 0
    assert evidence["verification"]["returncode"] == 0
    assert isinstance(evidence["verification"]["output"], list)
    rendered = EVIDENCE.read_text(encoding="utf-8").lower()
    assert not any(marker in rendered for marker in ("password", "secret", "token", "vault", "ansible"))
    print("POC evidence schema: status, verification, checksum, report, and secret-free fields validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
