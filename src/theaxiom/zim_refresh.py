"""Refresh Kiwix's private manifest after publication changes, without shelling out.

Only the separately installed loopback Kiwix sidecar may serve archives.
Publication is fail-closed while its OPDS state catches up. This module
never starts, stops or reloads a service and never changes storage modes.
"""
import logging
import threading
import time

from . import publication, zim_catalog, zim_reader_probe

RETRIES = 40
INTERVAL = 0.5
LOGGER = logging.getLogger(__name__)


def reconcile(load_records, *, attempts=RETRIES, interval=INTERVAL):
    """Refresh the manifest and acknowledge only the exact loaded book set."""
    failure = "readiness-validation-failed"
    for attempt in range(attempts):
        try:
            with publication.LOCK:
                records = load_records()
                expected = zim_catalog.render_xml(records)
                target = zim_catalog.manifest_path()
                try:
                    current = target.read_bytes()
                except FileNotFoundError:
                    current = None
                if current != expected:
                    zim_catalog.write_manifest(records)
                if b"<book " not in expected:
                    zim_catalog.activation_failure = None
                    return True  # nothing can be served; old marker is invalid
                # Publication status is currently gated until this succeeds.
                try:
                    zim_reader_probe.acknowledge(records)
                except zim_reader_probe.ReaderUnavailable:
                    zim_catalog.activation_failure = (expected, "Kiwix service unavailable")
                    raise
                except (OSError, ValueError):
                    zim_catalog.activation_failure = (
                        (expected, "Kiwix activation timed out") if attempt + 1 == attempts else None
                    )
                    raise
                zim_catalog.activation_failure = None
                # Journal recovery is advisory to reader readiness: an
                # unavailable activity DB must never trigger an archive rollback.
                _recover_activity(records)
                return True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # A missing sidecar or a pending Kiwix --monitorLibrary reload is
            # expected during setup. Never bypass publication readiness.
            failure = zim_reader_probe._failure_category(exc)
        if attempt + 1 < attempts:
            time.sleep(interval)
    LOGGER.warning("Kiwix publication reconciliation timed out attempts=%d reason=%s", attempts, failure)
    return False


def _recover_activity(records):
    from . import zim_jobs
    try:
        enabled = publication.load()
        for record in records:
            if not isinstance(record, dict):
                continue
            sid = record["source_id"]
            if enabled.get(sid) and record["configuration"].get("format") == "zim":
                candidate = zim_jobs.read_candidate(sid)
                if candidate and candidate.get("activity_id") and candidate["state"] == "activated":
                    zim_jobs.confirm_activation(sid, candidate["configuration"])
    except (OSError, ValueError, KeyError, TypeError):
        LOGGER.warning("Unable to reconcile committed source activity; will retry")


def reconcile_activation(load_records, *, source_id, configuration,
                         attempts=RETRIES, interval=INTERVAL):
    """Fail safe after a verified replacement cannot become reader-ready.

    Roll back only the exact activated candidate, using the same durable
    compare-and-swap safety checks as an explicit rollback. Do not delete
    either installed archive. Refresh the restored reader manifest afterward.
    """
    from . import zim_jobs
    if reconcile(load_records, attempts=attempts, interval=interval):
        zim_jobs.confirm_activation(source_id, configuration)
        return True
    try:
        zim_jobs.rollback_candidate(
            source_id, configuration,
            reason="Replacement reader verification failed; previous verified archive restored",
        )
    except (BlockingIOError, OSError, ValueError, TypeError, KeyError):
        # The serving identity may have changed while reconciliation ran.
        # Never overwrite a divergent job or manufacture a rollback.
        return False
    reconcile(load_records, attempts=attempts, interval=interval)
    return False


def schedule(load_records, *, activated_source=None, activated_configuration=None):
    """One daemon thread per change; never block HTTP or delete old archives."""
    if activated_source is None:
        target = reconcile
        kwargs = {}
    else:
        if not isinstance(activated_source, str) or not isinstance(activated_configuration, dict):
            raise ValueError("Activation recovery requires the exact source configuration")
        target = reconcile_activation
        kwargs = {"source_id": activated_source,
                  "configuration": dict(activated_configuration)}
    worker = threading.Thread(target=target, args=(load_records,), kwargs=kwargs,
                              name="kiwix-publication-sync", daemon=True)
    worker.start()
    return worker
