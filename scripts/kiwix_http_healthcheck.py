#!/usr/bin/env python3
"""Check the three bounded HTTP endpoints required by the offline POC."""

import argparse
import json
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

CHECKS = (
    ("homepage", "/", "Emergency reference library"),
    ("kiwix_catalog", "/kiwix/", "Emergency reference library"),
    ("fixture", "/content/offline-library-fixture", "Vancouver Offline Library POC fixture"),
)


def check(base_url: str, retries: int, timeout: float, opener=urlopen) -> dict:
    base_url = base_url.rstrip("/")
    results = []
    for name, path, expected in CHECKS:
        last_error = ""
        passed = False
        status = None
        for attempt in range(1, retries + 1):
            try:
                request = Request(base_url + path, headers={"Accept": "text/html"})
                with opener(request, timeout=timeout) as response:
                    status = response.status
                    body = response.read().decode("utf-8", errors="replace")
                if status != 200:
                    last_error = f"expected status 200, got {status}"
                elif expected not in body:
                    last_error = f"missing expected content: {expected!r}"
                else:
                    passed = True
                    break
            except (OSError, URLError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.05)
        results.append({"name": name, "path": path, "status": status, "ok": passed, **({"error": last_error} if not passed else {})})
    return {"base_url": base_url, "ok": all(item["ok"] for item in results), "checks": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="HTTP origin, for example http://192.168.0.4")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    if args.retries < 1 or args.timeout <= 0:
        parser.error("--retries must be positive and --timeout must be greater than zero")
    result = check(args.base_url, args.retries, args.timeout)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
