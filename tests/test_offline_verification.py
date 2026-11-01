#!/usr/bin/env python3
"""Verify the offline checker is local-only and deterministic."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from offline_verification import verify


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        pages = {
            "/": "Emergency reference library",
            "/kiwix/": "Emergency reference library",
            "/content/offline-library-fixture": "Vancouver Offline Library POC fixture",
        }
        body = pages.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write((body or "missing").encode())

    def log_message(self, format, *args):
        del format, args


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = verify(f"http://127.0.0.1:{server.server_port}", retries=1, timeout=1)
        assert result["ok"] is True
        assert result["dns_or_wan_access"] is False
        assert [item["status"] for item in result["checks"]] == [200, 200, 200]
        assert json.dumps(result, sort_keys=True, separators=(",", ":")) == json.dumps(result, sort_keys=True, separators=(",", ":"))
        try:
            verify("https://example.com", retries=1, timeout=1)
        except ValueError:
            pass
        else:
            raise AssertionError("non-local endpoint was accepted")
    finally:
        server.shutdown()
        server.server_close()
    print("Offline verification: local fixture passed; DNS/WAN endpoint rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
