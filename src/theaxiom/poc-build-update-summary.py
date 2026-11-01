#!/usr/bin/env python3
"""Render a concise human-readable summary from a validated POC report."""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    readiness = report.get("suite", {}).get("report")
    if readiness is not None:
        report = readiness
    verification = report["http_verification"]
    print(f"POC fixture: {report['fixture_checksum']}")
    print(f"Service: {report['service_version']}")
    print(f"Catalog: {report['catalog_result']}")
    print(f"HTTP fixture: {verification['fixture_status']} ({verification['fixture_body_sha256']})")
    print(f"Transfer performed: {'yes' if report['transfer_performed'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
