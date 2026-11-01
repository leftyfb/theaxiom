#!/usr/bin/env python3
"""Exercise HTTP health checks against a local deterministic test service."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kiwix_http_healthcheck import check


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        pages = {
            "/": "<h1>Emergency reference library</h1>",
            "/kiwix/": "<title>Emergency reference library</title>",
            "/content/offline-library-fixture": "Vancouver Offline Library POC fixture",
        }
        body = pages.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, format, *args):
        del format, args


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = check(f"http://127.0.0.1:{server.server_port}", retries=2, timeout=1)
        assert result["ok"]
        assert [item["status"] for item in result["checks"]] == [200, 200, 200]
        assert json.loads(json.dumps(result, sort_keys=True))["ok"] is True
        print("HTTP health checks: homepage, Kiwix catalog, and fixture page passed locally")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
