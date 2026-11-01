#!/usr/bin/env python3
"""Focused test for scripts/source_probe.py with synthetic fixtures.

Covers three states:
  - reachable + matching content-length → local_current True
  - unreachable (connection refused) → local_current False, error recorded
  - reachable but mismatched content-length → local_current False
"""

import http.server
import threading
from pathlib import Path
from urllib.request import urlopen

import source_probe

ROOT = Path(__file__).resolve().parents[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):  # noqa: N802
        if self.path == "/current":
            self.send_response(200)
            self.send_header("Content-Length", "754723")
            self.send_header("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT")
            self.end_headers()
        elif self.path == "/stale":
            self.send_response(200)
            self.send_header("Content-Length", "999999")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        del format, args


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        # 1. Reachable + matching content-length → current
        current = source_probe.probe_source(
            f"http://127.0.0.1:{port}/current",
            "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5",
            754723,
            timeout=2.0,
            opener=urlopen,
        )
        assert current["reachable"] is True, current
        assert current["local_current"] is True, current
        assert current["last_modified"] == "Wed, 21 Oct 2015 07:28:00 GMT", current
        assert current["content_length"] == "754723", current

        # 2. Reachable but mismatched content-length → not current
        stale = source_probe.probe_source(
            f"http://127.0.0.1:{port}/stale",
            "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5",
            754723,
            timeout=2.0,
            opener=urlopen,
        )
        assert stale["reachable"] is True, stale
        assert stale["local_current"] is False, stale

        # 3. Unreachable (connection refused) → not reachable
        unreachable = source_probe.probe_source(
            "http://127.0.0.1:1/",
            "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5",
            754723,
            timeout=0.5,
            opener=urlopen,
        )
        assert unreachable["reachable"] is False, unreachable
        assert unreachable["local_current"] is False, unreachable
        assert unreachable["error"], "expected error for unreachable probe"

        # 4. probe_all returns a list with one entry per manifest document
        results = source_probe.probe_all(timeout=0.5, opener=urlopen)
        assert isinstance(results, list), results
        assert len(results) == 3, f"expected 3 sources, got {len(results)}"
        for item in results:
            assert "id" in item and "title" in item, item
            assert "reachable" in item and "local_current" in item, item
        print("source_probe contract: PASS")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
