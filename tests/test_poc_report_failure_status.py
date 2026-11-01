#!/usr/bin/env python3
"""Verify a non-200 probe response cannot produce a passed catalog result."""

import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "src/theaxiom/poc-build-update-report.py"
BODY = b"unavailable fixture body\n"


def main() -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            subprocess.run([
                "python3", str(GENERATOR), "--deployed-checksum",
                hashlib.sha256(BODY).hexdigest(), "--service-version", "fixture-poc/2.0",
                "--catalog-result", "passed",
                "--probe-url", f"http://127.0.0.1:{server.server_port}/fixture",
                "--output", str(output),
            ], check=True)
            report = json.loads(output.read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        thread.join()

    assert report["catalog_result"] == "failed"
    assert report["http_verification"]["fixture_status"] == 204
    print("POC failure status: non-200 probe forces catalog failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
