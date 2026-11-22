"""Single-pass, read-only scheduled publisher metadata checks.

Intentionally separate from the HTTP server. No download, update, publication,
deletion, or service restart is performed by this runner.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

from . import (catalog_store, document_update_discovery, kiwix_update_discovery,
               publication, storage_settings)

DEFAULT_MAX_CHECKS = 10
MAX_CHECKS = 20


def check_records(records, *, max_checks=DEFAULT_MAX_CHECKS, start_index=0, checker=None):
    """Bound a single sweep of source-specific metadata-only update checks."""
    if type(max_checks) is not int or not 1 <= max_checks <= MAX_CHECKS:
        raise ValueError("Invalid maximum source check count")
    if type(start_index) is not int or start_index < 0:
        raise ValueError("Invalid start index")
    if not isinstance(records, list):
        raise ValueError("Invalid source catalog")
    supported = {"zim", "pdf", "epub"}
    by_id = {
        record["source_id"]: record for record in records
        if isinstance(record, dict)
        and isinstance(record.get("source_id"), str)
        and record["source_id"] not in publication.ROUTES
        and isinstance(record.get("configuration"), dict)
        and record["configuration"].get("format") in supported
        and (record["configuration"].get("format") == "zim"
             or record["configuration"].get("source_url"))
    }
    eligible = sorted(by_id)
    results = []
    offset = start_index % len(eligible) if eligible else 0
    selected = (eligible[offset:] + eligible[:offset])[:max_checks]
    for sid in selected:
        try:
            if checker is not None:
                outcome = checker(sid)
            elif by_id[sid]["configuration"]["format"] == "zim":
                outcome = kiwix_update_discovery.refresh_checked_source(sid)
            else:
                outcome = document_update_discovery.check_installed_document(by_id[sid])
            state = outcome.get("state") if isinstance(outcome, dict) else None
        except (OSError, ValueError, TypeError, KeyError):
            state = None
        results.append({"source_id": sid,
                        "state": state if state in {
                            "update_available", "up_to_date", "older_candidate",
                             "no_newer_edition_listed",
                            "representation_changed", "representation_unchanged",
                            "unable_to_determine"} else "unable_to_determine"})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only publisher update checks for installed ZIM/PDF/EPUB")
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
        # Rotate the bounded daily window so larger libraries are not
        # permanently starved by the per-run network/time budget.
        start_index = date.today().toordinal() * args.max_checks
        results = check_records(catalog["sources"], max_checks=args.max_checks,
                                start_index=start_index)
    except (ValueError, OSError, catalog_store.CatalogError) as exc:
        print("Source catalog/update check unavailable; no content modified", file=sys.stderr)
        return 2
    print(json.dumps({"checks": results}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
