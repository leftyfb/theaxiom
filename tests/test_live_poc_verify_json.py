#!/usr/bin/env python3
"""Test the standalone POC verifier's machine-readable output mode."""

import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/theaxiom/live-poc-verify.py"


def test_live_poc_verify_json() -> None:
    main()


def main() -> int:
    body = b"standalone offline fixture\n"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            del format, args

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "fixture"
        fixture.write_bytes(body)
        checksum = hashlib.sha256(body).hexdigest()
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "fixture_checksum": checksum,
                    "http_verification": {
                        "fixture_status": 200,
                        "fixture_body_sha256": checksum,
                    },
                    "catalog_result": "passed",
                }
            ),
            encoding="utf-8",
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    "python3",
                    str(COMMAND),
                    "--fixture",
                    str(fixture),
                    "--report",
                    str(report),
                    "--url",
                    f"http://127.0.0.1:{server.server_port}/fixture",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            server.shutdown()
            thread.join()

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert set(payload["checks"]) == {
        "homepage_status",
        "fixture_status",
        "deployed_checksum",
        "report_http_status",
        "report_body_checksum",
        "report_catalog_result",
    }
    assert all(payload["checks"].values())
    assert result.stderr == ""
    print("Live POC verifier JSON: machine-readable standalone checks emitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
