"""Metadata-only scheduled publisher sweeps with per-source deadlines."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock, Thread

from . import (catalog_store, document_update_discovery, kiwix_update_discovery,
               storage_settings, update_check_store, update_diagnostics,
               update_discovery_config, update_schedule)

LOGGER = logging.getLogger(__name__)
WORKERS = 8
SOURCE_TIMEOUT = 60
MANUAL_LOCK = Lock()
MANUAL_STATUS = {"state": "idle", "checks": []}


def _check_source(record, *, manual=False):
    sid = record["source_id"]
    config = record["configuration"]
    with update_diagnostics.attempt(sid, update_discovery_config.settings(config),
                                    trigger="manual" if manual else "scheduled") as diagnostic:
        if config["format"] == "zim":
            outcome = kiwix_update_discovery.refresh_checked_source(sid, force=manual, installed_only=True)
        else:
            outcome = document_update_discovery.check_installed_document(record, installed_only=True)
        diagnostic.update(outcome or {})
        return outcome


def _check_with_deadline(record, *, manual):
    # A subprocess also bounds DNS, filesystem and publisher stalls that can
    # outlive individual transport timeouts.
    result = subprocess.run(
        [sys.executable, "-m", "theaxiom.kiwix_update_scheduler", "--worker"],
        input=json.dumps({"record": record, "manual": manual}), stdout=subprocess.PIPE, text=True,
        check=True, timeout=SOURCE_TIMEOUT,
    )
    return json.loads(result.stdout)


def check_records(records, *, manual=False):
    eligible = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        sid, config = record.get("source_id"), record.get("configuration")
        if (isinstance(sid, str) and isinstance(config, dict)
                and config.get("format") in {"zim", "pdf", "epub"}):
            discovery = update_discovery_config.settings(config)
            if discovery["periodic"] or (manual and discovery["method"] != "manual"):
                eligible[sid] = record

    def check(sid):
        state = "unable_to_determine"
        try:
            outcome = _check_with_deadline(eligible[sid], manual=manual)
            if outcome is None:
                return None
            if outcome.get("state") in update_check_store.STATES:
                state = outcome["state"]
        except Exception as exc:
            LOGGER.warning("Scheduled metadata check failed for %r (%s)", sid, type(exc).__name__)
        return {"source_id": sid, "state": state}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return [item for item in pool.map(check, sorted(eligible)) if item is not None]


def _run(path, *, custom=False, manual=False):
    lock_path = update_schedule.state_path().with_suffix(".lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        schedule = update_schedule.current()
        if not manual and bool(schedule) != custom:
            return
        due = None
        if custom and schedule:
            due, _ = update_schedule.slot(schedule, datetime.now(update_schedule.timezone()))
            if due is None:
                return
        catalog = catalog_store.CatalogStore(path).get(initialize=False)
        results = check_records(catalog["sources"], manual=manual)
        if due:
            update_schedule.complete(schedule, due)
        print(json.dumps({"checks": results}, separators=(",", ":")), flush=True)
        return results


def _catalog_path():
    return Path(os.environ.get("THEAXIOM_SOURCE_CATALOG",
                               storage_settings.SETTINGS_PATH.with_name("sources.json")))


def manual_status():
    with MANUAL_LOCK:
        return dict(MANUAL_STATUS)


def start_manual():
    with MANUAL_LOCK:
        if MANUAL_STATUS["state"] == "running":
            return False
        MANUAL_STATUS.update(state="running", checks=[])

    def run():
        outcome = {"state": "failed", "checks": []}
        try:
            path = _catalog_path()
            if not path.is_file() or path.is_symlink():
                raise ValueError("Source catalog unavailable")
            results = _run(path, manual=True)
            if results is None:
                outcome["state"] = "busy"
            else:
                outcome = {"state": "completed", "checks": results}
        except (ValueError, KeyError, TypeError, OSError):
            LOGGER.exception("Manual update sweep unavailable")
        finally:
            with MANUAL_LOCK:
                MANUAL_STATUS.update(outcome)

    Thread(target=run, name="manual-update-sweep", daemon=True).start()
    return True


def start():
    """Poll the custom schedule from the admin service, across restarts."""
    shutdown = Event()

    def poll():
        while not shutdown.is_set():
            try:
                path = _catalog_path()
                if path.is_file() and not path.is_symlink() and update_schedule.current():
                    _run(path, custom=True)
            except (ValueError, KeyError, TypeError, OSError):
                LOGGER.warning("Custom update schedule or source catalog unavailable", exc_info=True)
            shutdown.wait(15)

    Thread(target=poll, name="update-schedule", daemon=True).start()
    return shutdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        payload = json.load(sys.stdin)
        print(json.dumps(_check_source(payload["record"], manual=payload["manual"])))
        return 0
    path = _catalog_path()
    if not path.is_file() or path.is_symlink():
        print("Source catalog unavailable; no metadata checks performed", file=sys.stderr)
        return 2
    try:
        _run(path, custom=False)
    except (ValueError, KeyError, TypeError, OSError):
        print("Source catalog/update check unavailable; no content modified", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
