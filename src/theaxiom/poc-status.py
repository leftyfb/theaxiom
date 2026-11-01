#!/usr/bin/env python3
"""Emit concise local-only status for the Vancouver fixture POC."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures/kiwix/tiny.txt")
    parser.add_argument("--report", type=Path, default=ROOT / "fixtures/kiwix/poc-build-update-report.json")
    parser.add_argument("--service", help="optional systemd service name to check")
    args = parser.parse_args()

    service_requested = args.service is not None
    service = None
    if service_requested:
        try:
            service = subprocess.run(
                ["systemctl", "is-active", args.service],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            service = None
    report = json.loads(args.report.read_text(encoding="utf-8"))
    checksum = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    status = {
        "service": {"name": args.service, "requested": service_requested, "checked": service is not None, "active": service.returncode == 0 if service else None, "state": service.stdout.strip() if service else ("systemctl unavailable" if service_requested else "not checked")},
        "fixture": {"path": str(args.fixture), "sha256": checksum},
        "report": {
            "path": str(args.report),
            "service_version": report.get("service_version"),
            "catalog_result": report.get("catalog_result"),
            "fixture_checksum": report.get("fixture_checksum"),
            "transfer_performed": report.get("transfer_performed"),
        },
    }
    print(json.dumps(status, separators=(",", ":"), sort_keys=True))
    healthy = (
        (
            not status["service"]["requested"]
            or (status["service"]["checked"] and status["service"]["active"] is True)
        )
        and status["fixture"]["sha256"] == status["report"]["fixture_checksum"]
        and status["report"]["catalog_result"] == "passed"
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
