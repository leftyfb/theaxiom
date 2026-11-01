#!/usr/bin/env python3
"""Verify the deployed Vancouver fixture POC without changing the host."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures/kiwix/tiny.txt")
    parser.add_argument("--report", type=Path, default=ROOT / "fixtures/kiwix/poc-build-update-report.json")
    parser.add_argument("--url", default="http://127.0.0.1/content/offline-library-fixture")
    parser.add_argument("--service", help="optional systemd service name to check")
    parser.add_argument("--json", action="store_true", help="emit machine-readable verification results")
    args = parser.parse_args()
    service_ok = subprocess.run(["systemctl", "is-active", "--quiet", args.service], check=False).returncode == 0 if args.service else None
    fixture_checksum = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    with urlopen(args.url) as response:
        body = response.read()
        status = response.status
    report = json.loads(args.report.read_text(encoding="utf-8"))
    checks = {
        "homepage_status": status == 200, "fixture_status": status == 200,
        "deployed_checksum": report.get("fixture_checksum") == fixture_checksum,
        "report_http_status": report.get("http_verification", {}).get("fixture_status") == status,
        "report_body_checksum": report.get("http_verification", {}).get("fixture_body_sha256") == hashlib.sha256(body).hexdigest(),
        "report_catalog_result": report.get("catalog_result") == "passed" and status == 200,
    }
    if args.service:
        checks["service_active"] = service_ok
    if args.json:
        print(json.dumps({"checks": checks, "passed": all(checks.values())}, sort_keys=True))
    else:
        for name, passed in checks.items():
            print(f"{name}: {'passed' if passed else 'failed'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
