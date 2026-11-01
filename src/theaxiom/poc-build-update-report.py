#!/usr/bin/env python3
"""Generate the deterministic machine-readable POC report from local probes."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployed-checksum", required=True)
    parser.add_argument("--service-version", required=True)
    parser.add_argument("--catalog-result", choices=("passed", "failed"), required=True)
    parser.add_argument("--probe-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with urlopen(args.probe_url) as response:
        body = response.read()
        http_status = response.status
    catalog_result = "passed" if http_status == 200 and args.catalog_result == "passed" else "failed"
    report = {"fixture_checksum": args.deployed_checksum, "service_version": args.service_version,
              "catalog_result": catalog_result,
              "http_verification": {"fixture_status": http_status,
                                    "fixture_body_sha256": hashlib.sha256(body).hexdigest()},
              "transfer_performed": False}
    rendered = json.dumps(report, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() and args.output.read_text(encoding="utf-8") == rendered:
        print("unchanged")
    else:
        args.output.write_text(rendered, encoding="utf-8")
        print("changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
