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


def schedule(load_records):
    """One daemon thread per explicit publication change; never block HTTP."""
    worker = threading.Thread(target=reconcile, args=(load_records,),
                              name="kiwix-publication-sync", daemon=True)
    worker.start()
    return worker
