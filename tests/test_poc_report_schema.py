#!/usr/bin/env python3
"""Validate the machine-readable Vancouver POC report schema and fixture."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/poc-build-update-report.schema.json"
REPORT = ROOT / "fixtures/kiwix/poc-build-update-report.json"


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    required = {"fixture_checksum", "service_version", "catalog_result", "http_verification", "transfer_performed"}
    assert set(schema["required"]) == required
    assert set(report) == required
    checksum = report["fixture_checksum"]
    assert re.fullmatch(r"[a-f0-9]{64}", checksum)
    assert report["service_version"]
    assert report["catalog_result"] == "passed"
    verification = report["http_verification"]
    assert verification["fixture_status"] == 200
    assert verification["fixture_body_sha256"] == checksum
    assert report["transfer_performed"] is False
    print("POC report schema: required checksum, version, catalog, HTTP, and no-transfer fields validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
