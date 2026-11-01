#!/usr/bin/env python3
"""Validate the standalone POC verifier with synthetic local fixtures."""

import functools
import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "src/theaxiom/live-poc-verify.py"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        del format, args


def test_live_poc_verify() -> None:
    main()


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        body = b"synthetic standalone fixture\n"
        fixture.write_bytes(body)
        checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "fixture_checksum": checksum,
                    "catalog_result": "passed",
                    "http_verification": {
                        "fixture_status": 200,
                        "fixture_body_sha256": checksum,
                    },
                }
            ),
            encoding="utf-8",
        )
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(root))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    "python3",
                    str(VERIFY),
                    "--fixture",
                    str(fixture),
                    "--report",
                    str(report),
                    "--url",
                    f"http://127.0.0.1:{server.server_port}/fixture",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "homepage_status: passed" in result.stdout
            assert "fixture_status: passed" in result.stdout
            assert "deployed_checksum: passed" in result.stdout
            assert "report_http_status: passed" in result.stdout
            assert "report_body_checksum: passed" in result.stdout
            assert "report_catalog_result: passed" in result.stdout
            assert "service_active" not in result.stdout
        finally:
            server.shutdown()
            thread.join()
    print("Live POC verifier: synthetic HTTP, checksum, and report checks validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
