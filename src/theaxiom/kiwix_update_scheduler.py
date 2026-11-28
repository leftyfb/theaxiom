"""Metadata-only scheduled publisher sweeps with per-source deadlines."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock, Thread
from hashlib import sha256

from . import (catalog_store, document_replacement, document_update_discovery, kiwix_update_discovery, kolibri_admin,
               storage_settings, update_check_store, update_diagnostics,
               update_discovery_config as discovery, update_schedule, zim_jobs)

LOGGER = logging.getLogger(__name__)
WORKERS = 8
SOURCE_TIMEOUT = 60
MANUAL_LOCK = Lock()
MANUAL_STATUS = {"state": "idle", "checks": []}
SOURCE_RETRIES = 3
SOURCE_BACKOFF = 300


def _check_source(record, *, manual=False, unattended=None):
    sid = record["source_id"]
    config = record["configuration"]
    if unattended:
        records = catalog_store.CatalogStore(unattended["path"]).get(initialize=False)["sources"]
        current = next((row for row in records if row["source_id"] == sid), None)
        if not current or current["configuration"] != config or update_schedule.automatic_paused():
            return None
        now = datetime.now(update_schedule.timezone())
        if not discovery.window_open(config, now) or _window(config, now) != unattended["window"]:
            return None
    trigger = "manual" if manual else ("automatic" if unattended and discovery.policy(config)[0] == "automatic" else "scheduled")
    with update_diagnostics.attempt(sid, discovery.settings(config),
                                    trigger=trigger) as diagnostic:
        if config["format"] == "kolibri":
            outcome = kolibri_admin.check_update(kolibri_admin.configured(), record)
        elif config["format"] in {"zim", "pmtiles"}:
            options = {"expected_identity": unattended["installed"]} if unattended else {}
            outcome = kiwix_update_discovery.refresh_checked_source(
                sid, force=manual or bool(unattended), installed_only=True, **options)
        else:
            if unattended and document_replacement.installed(record) != unattended["installed"]:
                return None
            outcome = document_update_discovery.check_installed_document(record, installed_only=True)
        diagnostic.update(outcome or {})
        return outcome


def _check_with_deadline(record, *, manual, unattended=None):
    # A subprocess also bounds DNS, filesystem and publisher stalls that can
    # outlive individual transport timeouts.
    result = subprocess.run(
        [sys.executable, "-m", "theaxiom.kiwix_update_scheduler", "--worker"],
        input=json.dumps({"record": record, "manual": manual, "unattended": unattended}),
        stdout=subprocess.PIPE, text=True,
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
                and config.get("format") in {"zim", "pdf", "epub", "pmtiles", "kolibri"}):
            selected = discovery.settings(config)
            if selected["periodic"] or (manual and selected["method"] != "manual"):
                eligible[sid] = record

    def check(sid):
        state = "unable_to_determine"
        evidence = None
        try:
            outcome = _check_with_deadline(eligible[sid], manual=manual)
            if outcome is None:
                return None
            if outcome.get("state") in update_check_store.STATES:
                state = outcome["state"]
                if state == "unable_to_determine":
                    evidence = outcome.get("evidence")
        except subprocess.TimeoutExpired:
            evidence = "Publisher metadata check exceeded the per-source deadline."
            LOGGER.warning("Scheduled metadata check timed out for %r", sid)
        except Exception as exc:
            evidence = "Metadata check failed; see the server update-check log."
            LOGGER.warning("Scheduled metadata check failed for %r (%s)", sid, type(exc).__name__)
        result = {"source_id": sid, "state": state}
        if evidence:
            result["evidence"] = evidence
        return result

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return [item for item in pool.map(check, sorted(eligible)) if item is not None]


@contextmanager
def check_lock():
    lock_path = update_schedule.state_path().with_suffix(".lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def _window(config, now):
    if discovery.policy(config)[0] == "scheduled":
        return discovery.window_start(config, now).isoformat()
    return None


def _retry_path(sid):
    return update_check_store.record_path(sid).with_suffix(".unattended.json")


def _retry_key(config, installed, window):
    return sha256(json.dumps([config, installed, window], sort_keys=True).encode()).hexdigest()


def _retry_state(sid, key):
    try:
        state = json.loads(update_check_store.read_bytes(_retry_path(sid)) or b"null")
        if (state and state["key"] == key and type(state["count"]) is int
                and 1 <= state["count"] <= SOURCE_RETRIES and type(state["next_at"]) in (int, float)
                and 0 < state["next_at"] < 4102444800
                and state["outcome"] in ("checking", "failed", "skipped", "checked")):
            return state
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return {"key": key, "count": 0, "next_at": 0, "outcome": "waiting"}


def unattended_status(sid, config, job, now):
    installed = (job if config.get("format") in document_replacement.FORMATS else
                 kiwix_update_discovery.installed_identity_from_job(job, job.get("artifact") or {}))
    if not installed or not discovery.settings(config)["periodic"]:
        return ""
    state = _retry_state(sid, _retry_key(config, installed, _window(config, now)))
    if state["count"] >= SOURCE_RETRIES and (state["outcome"] != "checking" or now.timestamp() >= state["next_at"]):
        return "Source check retry limit reached"
    return {"checking": "Source check in progress or awaiting retry",
            "failed": "Source check failed; retry pending", "checked": "Source check completed",
            "skipped": "Source changed; waiting to recheck"}.get(state["outcome"], "")


def poll_unattended(path):
    """Check due installed sources without claiming or completing a global slot."""
    with check_lock() as locked:
        if not locked or update_schedule.automatic_paused():
            return
        records = catalog_store.CatalogStore(path).get(initialize=False)["sources"]
        due = []
        for record in records:
            now = datetime.now(update_schedule.timezone())
            sid, config = record["source_id"], record["configuration"]
            if not discovery.window_open(config, now) or not discovery.settings(config)["periodic"]:
                continue
            if config.get("format") in document_replacement.FORMATS:
                installed = document_replacement.installed(record)
                if not installed:
                    continue
            else:
                job = zim_jobs.read(sid)
                if not job:
                    continue
                installed = kiwix_update_discovery.installed_identity_from_job(job, job.get("artifact") or {})
                if not installed or not zim_jobs.installed(job, job["configuration"]):
                    continue
            evidence = update_check_store.load(sid, discovery.cache_identity(config, installed["sha256"]), now=now.timestamp())
            if (discovery.current_unattended_evidence(config, now, evidence)
                    or (evidence.get("state") == "unable_to_determine"
                        and 0 <= now.timestamp() - evidence.get("checked_at", 0) < SOURCE_BACKOFF)):
                continue
            window = _window(config, now)
            state = _retry_state(sid, _retry_key(config, installed, window))
            if state["count"] >= SOURCE_RETRIES or now.timestamp() < state["next_at"]:
                continue
            # Persist before starting I/O so a service interruption cannot reset the budget.
            due.append((record, state | {"count": state["count"] + 1, "outcome": "checking",
                                         "next_at": now.timestamp() + SOURCE_BACKOFF},
                        {"path": str(path), "window": window, "installed": installed}))
            if len(due) == WORKERS:
                break

        def check(item):
            record, state, context = item
            sid = record["source_id"]
            target = _retry_path(sid)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            update_schedule._write(state, target)
            outcome = None
            try:
                outcome = _check_with_deadline(record, manual=False, unattended=context)
                state["outcome"] = ("skipped" if outcome is None else
                                    "failed" if outcome["state"] == "unable_to_determine" else "checked")
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                state["outcome"] = "failed"
                LOGGER.warning("Unattended metadata check failed source_id=%r exception_type=%s", sid, type(exc).__name__)
            LOGGER.info("Unattended metadata check source_id=%r window=%s attempt=%d outcome=%s state=%s",
                        sid, context["window"], state["count"], state["outcome"], (outcome or {}).get("state"))
            update_schedule._write(state, target)

        if due:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                list(pool.map(check, due))


def _run(path, *, custom=False, manual=False):
    with check_lock() as locked:
        if not locked:
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
            path = _catalog_path()
            try:
                if path.is_file() and not path.is_symlink() and update_schedule.current():
                    _run(path, custom=True)
            except (ValueError, KeyError, TypeError, OSError):
                LOGGER.warning("Custom update schedule or source catalog unavailable", exc_info=True)
            try:
                if path.is_file() and not path.is_symlink():
                    poll_unattended(path)
            except (ValueError, KeyError, TypeError, OSError):
                LOGGER.warning("Unattended source metadata scheduling unavailable", exc_info=True)
            shutdown.wait(15)

    Thread(target=poll, name="update-schedule", daemon=True).start()
    return shutdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        payload = json.load(sys.stdin)
        print(json.dumps(_check_source(payload["record"], manual=payload["manual"],
                                       unattended=payload.get("unattended"))))
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
