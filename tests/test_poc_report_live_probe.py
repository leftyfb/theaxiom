#!/usr/bin/env python3
"""Verify the POC report records an actual local HTTP probe response."""

import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "src/theaxiom/poc-build-update-report.py"
BODY = b"live fixture body\n"


def main() -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
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
            subprocess.run(["python3", str(GENERATOR), "--deployed-checksum", "a" * 64,
                            "--service-version", "fixture-poc/2.0", "--catalog-result", "passed",
                            "--probe-url", f"http://127.0.0.1:{server.server_port}/fixture",
                            "--output", str(output)], check=True)
            report = json.loads(output.read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        thread.join()
    assert report["http_verification"] == {
        "fixture_status": 200,
        "fixture_body_sha256": hashlib.sha256(BODY).hexdigest(),
    }
    print("POC live probe: report uses the local HTTP response status and body checksum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
