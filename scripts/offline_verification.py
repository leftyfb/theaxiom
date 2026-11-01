#!/usr/bin/env python3
"""Verify the offline POC endpoints without permitting DNS or WAN access."""

from __future__ import annotations

import argparse
import json
import urllib.parse

from kiwix_http_healthcheck import check

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def verify(base_url: str, retries: int = 3, timeout: float = 2.0) -> dict:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("base_url must use a local hostname (localhost, 127.0.0.1, or ::1)")
    result = check(base_url, retries=retries, timeout=timeout)
    return {
        "base_url": result["base_url"],
        "checks": result["checks"],
        "dns_or_wan_access": False,
        "ok": result["ok"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    if args.retries < 1 or args.timeout <= 0:
        parser.error("--retries must be positive and --timeout must be greater than zero")
    try:
        result = verify(args.base_url, args.retries, args.timeout)
    except (OSError, ValueError) as exc:
        result = {
            "base_url": args.base_url.rstrip("/"),
            "checks": [],
            "dns_or_wan_access": False,
            "error": str(exc),
            "ok": False,
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
