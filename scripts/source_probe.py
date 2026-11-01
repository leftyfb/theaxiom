#!/usr/bin/env python3
"""Probe each configured source's origin URL and compare against the local copy.

Reads the documents list from config/catalog.yaml, issues a HEAD request to each
source_url, and records:

  - reachable:      HTTP 2xx or 3xx redirect
  - last_modified:  Last-Modified header value, or empty string
  - content_length: Content-Length header value, or empty string
  - local_current:  True when the local copy's SHA-256 matches the
                    manifest sha256 AND the reported content length matches
                    the manifest bytes (when both are present)

The probe is read-only and makes no changes to the repository or host.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/catalog.yaml"
sys.path.insert(0, str(ROOT / "src/theaxiom"))
from freshness_probe import probe_documents, probe_source  # noqa: E402,F401


def _load_manifest() -> list[dict[str, Any]]:
    """Return the offline_library_poc_catalog_documents list from defaults."""
    import yaml

    data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return list(data.get("documents", []))


def probe_all(timeout: float = 5.0, opener=urllib.request.urlopen) -> list[dict[str, Any]]:
    """Probe every managed source in the catalog."""
    return probe_documents(_load_manifest(), timeout=timeout, opener=opener)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    results = probe_all(timeout=args.timeout)
    if args.json:
        print(json.dumps(results, sort_keys=True, indent=2))
    else:
        for item in results:
            state = "CURRENT" if item.get("local_current") else ("REACHABLE" if item.get("reachable") else "UNREACHABLE")
            print(f"{state:12} {item.get('title','?'):45} {item.get('source_url','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
