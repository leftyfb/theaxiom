#!/usr/bin/env python3
"""Verify that POC status, catalog, and build/update report agree on one fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()

    status, catalog, report = (load(path) for path in (args.status, args.catalog, args.report))
    checksum = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    assert status["service"]["active"] is True and status["service"]["state"] == "active"
    assert status["report"]["catalog_result"] == report["catalog_result"] == "passed"
    assert status["fixture"]["sha256"] == report["fixture_checksum"] == catalog["sha256"] == checksum
    assert report["transfer_performed"] is False
    print("POC artifact consistency: status, catalog, report, and fixture agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
