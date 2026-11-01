#!/usr/bin/env python3
"""Verify unchanged POC probe inputs produce identical reports."""

import hashlib
import json
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "src/theaxiom/poc-build-update-report.py"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"offline fixture\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        checksum = hashlib.sha256(b"offline fixture\n").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            common = [
                "--deployed-checksum", checksum,
                "--service-version", "fixture-poc/2.0",
                "--catalog-result", "passed",
                "--probe-url", f"http://127.0.0.1:{server.server_port}/fixture",
            ]
            for output in (first, second):
                subprocess.run(["python3", str(GENERATOR), *common, "--output", str(output)], check=True)
            assert first.read_bytes() == second.read_bytes()
            assert json.loads(first.read_text(encoding="utf-8"))["transfer_performed"] is False
    finally:
        server.shutdown()
        thread.join()
    print("POC report idempotency: unchanged probe inputs produce identical reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
