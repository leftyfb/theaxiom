"""Single-pass, read-only scheduled publisher metadata checks.

Intentionally separate from the HTTP server. No download, update, publication,
deletion, or service restart is performed by this runner.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import catalog_store, kiwix_update_discovery, publication, storage_settings

DEFAULT_MAX_CHECKS = 10
MAX_CHECKS = 20


def check_records(records, *, max_checks=DEFAULT_MAX_CHECKS, checker=None):
    """Bound a single sweep; only installed ZIMs can yield actionable evidence."""
    if type(max_checks) is not int or not 1 <= max_checks <= MAX_CHECKS:
        raise ValueError("Invalid maximum source check count")
    if not isinstance(records, list):
        raise ValueError("Invalid source catalog")
    checker = kiwix_update_discovery.refresh_checked_source if checker is None else checker
    eligible = sorted(
        {record["source_id"] for record in records
         if isinstance(record, dict)
         and isinstance(record.get("source_id"), str)
         and record["source_id"] not in publication.ROUTES
         and isinstance(record.get("configuration"), dict)
         and record["configuration"].get("format") == "zim"}
    )
    results = []
    for sid in eligible[:max_checks]:
        try:
            outcome = checker(sid)
            state = outcome.get("state") if isinstance(outcome, dict) else None
        except (OSError, ValueError, TypeError, KeyError):
            state = None
        results.append({"source_id": sid,
                        "state": state if state in {
                            "update_available", "up_to_date", "older_candidate",
                            "unable_to_determine"} else "unable_to_determine"})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Kiwix publisher update checks")
    parser.add_argument("--max-checks", type=int, default=DEFAULT_MAX_CHECKS)
    args = parser.parse_args(argv)
    if not 1 <= args.max_checks <= MAX_CHECKS:
        parser.error("max-checks must be between 1 and 20")
    path = Path(os.environ.get(
        "THEAXIOM_SOURCE_CATALOG",
        storage_settings.SETTINGS_PATH.with_name("sources.json")))
    # A missing catalog is not permission to initialize a new one.
    if not path.is_file() or path.is_symlink():
        print("Source catalog unavailable; no metadata checks performed", file=sys.stderr)
        return 2
    try:
        catalog = catalog_store.CatalogStore(path).get()
        results = check_records(catalog["sources"], max_checks=args.max_checks)
    except (ValueError, OSError, catalog_store.CatalogError) as exc:
        print("Source catalog/update check unavailable; no content modified", file=sys.stderr)
        return 2
    print(json.dumps({"checks": results}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
