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
               publication, storage_settings, update_discovery_config, update_diagnostics)

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
        and isinstance(record.get("configuration"), dict)
        and record["configuration"].get("format") in supported
        and update_discovery_config.settings(record["configuration"])["periodic"]
    }
    eligible = sorted(by_id)
    results = []
    offset = start_index % len(eligible) if eligible else 0
    selected = (eligible[offset:] + eligible[:offset])[:max_checks]
    for sid in selected:
        discovery = update_discovery_config.settings(by_id[sid]["configuration"])
        with update_diagnostics.attempt(sid, discovery, trigger="scheduled") as diagnostic:
            try:
                if checker is not None:
                    outcome = checker(sid)
                elif sid in publication.ROUTES and by_id[sid]["configuration"]["format"] == "zim":
                    # Match the same independently verified immutable artifact and
                    # pinned-seed gate used by the authenticated manual check.
                    from . import acquisition_monitor, admin_api
                    record = by_id[sid]
                    artifact = admin_api.select_lifecycle(sid, record["configuration"]).get("artifact")
                    trusted = admin_api.TRUSTED_SOURCES.get(sid)
                    monitored = acquisition_monitor.collect(trusted) if trusted else None
                    verified = bool(
                        artifact and monitored and monitored.get("state") == "completed"
                        and monitored.get("verified") is True and monitored.get("active") is False
                        and all(monitored.get(field) == artifact[key] for field, key in (
                            ("filename", "filename"), ("expected_bytes", "bytes"),
                            ("sha256", "sha256")))
                    )
                    outcome = kiwix_update_discovery.refresh_pinned_seed(
                        record, artifact, verified=verified)
                elif by_id[sid]["configuration"]["format"] == "zim":
                    outcome = kiwix_update_discovery.refresh_checked_source(sid)
                else:
                    outcome = document_update_discovery.check_installed_document(by_id[sid])
                if isinstance(outcome, dict):
                    diagnostic.update(outcome)
                state = diagnostic.get("state")
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
