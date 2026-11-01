#!/usr/bin/env python3
"""Validate a captured POC evidence artifact against its build/update report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate(evidence: dict, report: dict) -> list[str]:
    errors: list[str] = []
    fixture_sha = evidence.get("fixture", {}).get("sha256")
    status = evidence.get("status", {})
    status_json = status.get("json", {})
    report_sha = report.get("fixture_checksum")
    if evidence.get("verification", {}).get("returncode") != 0:
        errors.append("verification returned nonzero")
    if status.get("returncode") != 0:
        errors.append("status returned nonzero")
    status_sha = status_json.get("fixture", {}).get("sha256")
    if status_sha != fixture_sha:
        errors.append("status fixture SHA-256 disagrees with evidence fixture SHA-256")
    if report_sha != fixture_sha:
        errors.append("build/update report checksum disagrees with evidence fixture SHA-256")
    if report.get("catalog_result") != "passed":
        errors.append("build/update report catalog result is not passed")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        errors = validate(load(args.evidence), load(args.report))
    except ValueError as exc:
        print(f"evidence validation: FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("evidence validation: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("evidence validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
