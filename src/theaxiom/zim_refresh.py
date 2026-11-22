"""Refresh Kiwix's private manifest after publication changes, without shelling out.

Only the separately installed loopback Kiwix sidecar may serve archives.
Publication is fail-closed while its OPDS state catches up. This module
never starts, stops or reloads a service and never changes storage modes.
"""
import threading
import time

from . import publication, zim_catalog, zim_reader_probe

RETRIES = 40
INTERVAL = 0.5


def reconcile(load_records, *, attempts=RETRIES, interval=INTERVAL):
    """Refresh the manifest and acknowledge only the exact loaded book set."""
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
                    return True  # nothing can be served; old marker is invalid
                # Publication status is currently gated until this succeeds.
                zim_reader_probe.acknowledge(records)
                return True
        except (OSError, ValueError, TypeError, KeyError):
            # A missing sidecar or a pending Kiwix --monitorLibrary reload is
            # expected during setup. Never bypass publication readiness.
            pass
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False


def reconcile_activation(load_records, *, source_id, configuration,
                         attempts=RETRIES, interval=INTERVAL):
    """Fail safe after a verified replacement cannot become reader-ready.

    Roll back only the exact activated candidate, using the same durable
    compare-and-swap safety checks as an explicit rollback. Do not delete
    either installed archive. Refresh the restored reader manifest afterward.
    """
    if reconcile(load_records, attempts=attempts, interval=interval):
        return True
    from . import zim_jobs
    try:
        zim_jobs.rollback_candidate(source_id, configuration)
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
