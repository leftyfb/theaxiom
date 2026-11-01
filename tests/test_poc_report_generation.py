#!/usr/bin/env python3
"""Verify deterministic standalone POC report generation."""

import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "src/theaxiom/poc-build-update-report.py"


def test_poc_report_generation() -> None:
    main()


def main() -> int:
    checksum = "a" * 64
    body = b"deterministic standalone report fixture\n"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            del format, args

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            command = [
                "python3",
                str(GENERATOR),
                "--deployed-checksum",
                checksum,
                "--service-version",
                "fixture-poc/2.0",
                "--catalog-result",
                "passed",
                "--probe-url",
                f"http://127.0.0.1:{server.server_port}/fixture",
                "--output",
                str(output),
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            report = json.loads(output.read_text(encoding="utf-8"))
            second = subprocess.run(command, check=True, capture_output=True, text=True)
    finally:
        server.shutdown()
        thread.join()

    assert first.stdout.strip() == "changed"
    assert second.stdout.strip() == "unchanged"
    assert report["fixture_checksum"] == checksum
    assert report["service_version"] == "fixture-poc/2.0"
    assert report["catalog_result"] == "passed"
    assert report["http_verification"] == {
        "fixture_status": 200,
        "fixture_body_sha256": hashlib.sha256(body).hexdigest(),
    }
    assert report["transfer_performed"] is False
    print("POC report generation: deterministic local report and no-transfer behavior validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
