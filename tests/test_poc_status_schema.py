#!/usr/bin/env python3
"""Validate the installed POC status JSON schema and synthetic fixture."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/poc-status.schema.json"
STATUS = ROOT / "fixtures/kiwix/poc-status.json"


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    status = json.loads(STATUS.read_text(encoding="utf-8"))
    assert schema["required"] == ["service", "fixture", "report"]
    assert set(status) == set(schema["required"])
    assert status["service"]["requested"] is False
    assert status["service"]["checked"] is False
    assert status["service"]["active"] is None
    assert status["service"]["state"] == "not checked"
    assert re.fullmatch(r"[a-f0-9]{64}", status["fixture"]["sha256"])
    report = status["report"]
    assert report["catalog_result"] == "passed"
    assert report["fixture_checksum"] == status["fixture"]["sha256"]
    assert report["transfer_performed"] is False
    print("POC status schema: service, fixture, report, checksum, and no-transfer fields validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
