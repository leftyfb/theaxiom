"""Admin-facing generic native Kolibri orchestration; disabled unless configured."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import threading

from . import kolibri_jobs, kolibri_native as native, kolibri_publication
from . import kolibri_scope as scope, pdf_source, storage_settings, update_check_store

LOCK = threading.RLock()
_INSTANCE = None
_PREVIEWS = threading.RLock()


def configured():
    global _INSTANCE
    registry = kolibri_publication.configured_registry()
    executable = os.environ.get("THEAXIOM_KOLIBRI_EXECUTABLE")
    home = os.environ.get("THEAXIOM_KOLIBRI_HOME")
    if not registry or not executable or not home:
        return None
    executable, home = Path(executable), Path(home)
    if (not executable.is_absolute() or not home.is_absolute() or not executable.is_file()
            or registry.database != home/"db.sqlite3"):
        return None
    settings = storage_settings.load_settings()
    if not settings["storage_root"]:
        return None
    root = Path(settings["storage_root"])/"library"/"kolibri"
    identity = (str(root), settings["storage_source"], settings["storage_mount"])
    with LOCK:
        if _INSTANCE is not None:
            previous, manager = _INSTANCE
            if previous == identity:
                return manager
            if manager.active or any(any(manager.read(path.stem).get(key) for key in ("installed", "partial")) for path in manager.state.glob("*.json")):
                raise ValueError("Existing Kolibri content must be resolved before changing storage")
        def guard(required):
            if storage_settings.load_settings() != settings:
                raise ValueError("Configured storage changed")
            with pdf_source._directory("library", writing=True, format_name="kolibri") as (fd, mount, minimum):
                capacity = os.fstatvfs(fd)
                if capacity.f_bavail*capacity.f_frsize < minimum+required:
                    raise ValueError("Insufficient configured storage capacity")
        manager = kolibri_jobs.Jobs(registry.root/"jobs", root/"packages", registry, executable, home, guard)
        _INSTANCE = identity, manager
        return manager


def channel(record):
    config = record["configuration"]
    if config.get("format") != "kolibri":
        raise ValueError("Not an educational source")
    return scope._id(config.get("kolibri_channel_id"))


def _preview_path(manager, cid, base):
    publisher = hashlib.sha256(native.validate_base(base).encode()).hexdigest()[:24]
    return manager.registry.root/"previews"/(scope._id(cid)+"-"+publisher+".sqlite3")


@contextmanager
def _preview_lock(manager, cid, base):
    path = _preview_path(manager, cid, base)
    with _PREVIEWS:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield path


def refresh(manager, record):
    cid = channel(record)
    with _preview_lock(manager, cid, native.source_base(record["configuration"])) as path:
        with tempfile.TemporaryDirectory(dir=path.parent) as folder:
            temp = Path(folder)/"preview.sqlite3"
            item, digest, size = native.fetch_metadata(cid, temp, base_url=native.source_base(record["configuration"]))
            os.replace(temp, path)
        return {"channel": asdict(item), "metadata_sha256": digest, "metadata_bytes": size,
                "children": [asdict(child) for child in scope.children(path, cid)]}


def browse(manager, record, parent=None):
    cid = channel(record)
    with _preview_lock(manager, cid, native.source_base(record["configuration"])) as path:
        item = scope.inspect_channel(path, cid)
        return {"channel": asdict(item), "children": [asdict(child) for child in scope.children(path, cid, parent)]}


def selection(manager, record, nodes, allow_full=False):
    cid = channel(record)
    with _preview_lock(manager, cid, native.source_base(record["configuration"])) as path:
        plan = scope.plan_scope(path, cid, nodes, allow_full=allow_full)
        with path.open("rb") as stream:
            value = hashlib.sha256()
            while block := stream.read(1024*1024):
                value.update(block)
            digest = value.hexdigest()
        selected = native.Selection(cid, tuple(plan.selected_nodes), plan.channel.version, digest,
                                    plan.total_bytes, allow_full, native.source_base(record["configuration"]))
        return selected, {"total_bytes": plan.total_bytes, "resources": plan.resource_count,
                          "licenses": list(plan.licenses), "missing_license_count": plan.missing_license_count,
                          "version": plan.channel.version, "metadata_sha256": digest}


def status(manager, record):
    if manager is None:
        return {"configured": False, "active": False}
    sid = record["source_id"]
    job = manager.read(sid)
    installed = job.get("installed")
    published = manager.registry.acknowledged().get(sid)
    selection = installed.get("selection") if installed else None
    if selection and selection["channel_id"] != channel(record):
        return {"configured": True, "error": "Installed channel does not match profile"}
    return {"configured": True, "active": sid in manager.active,
            "publication_phase": manager.publication_phase.get(sid),
            "recovery_pending": manager.registry.pending.exists(),
            "installed": selection, "published": bool(published),
            "reviewed": bool(job.get("reviewed")),
            "partial": bool(job.get("partial")),
            "partial_update": bool((job.get("partial") or {}).get("update")),
            "candidate": bool(job.get("candidate")), "previous": bool(job.get("previous")),
            "candidate_digest": (job.get("candidate") or {}).get("manifest_sha256"),
            "installed_digest": (installed or {}).get("manifest_sha256"),
            "job": {key: job[key] for key in ("state", "active", "downloaded_bytes", "expected_bytes",
                "sampled_at", "speed_bytes_per_second", "detail") if key in job}}


def start_download(manager, record, selected, *, update=False, edit_topics=False):
    sid = record["source_id"]
    def worker():
        try:
            manager.download(sid, selected, update=update, edit_topics=edit_topics, reserved=True)
        except (OSError, ValueError, RuntimeError):
            # The private job ledger records terminal failures.
            pass
    with manager.lock:
        if sid in manager.active:
            raise ValueError("Educational source is busy")
        job = manager.read(sid)
        if update:
            if not job.get("installed") or job.get("candidate") or job.get("previous"):
                raise ValueError("Resolve installed, staged or rollback content first")
            old = manager._selection(job["installed"])
            manager.validate_replacement(old, selected, edit_topics)
        elif job.get("installed"):
            raise ValueError("Educational content already installed")
        partial = job.get("partial")
        if partial and (manager._selection(partial) != selected or partial.get("update") != update):
            raise ValueError("Delete the interrupted package before changing its selection")
        manager.active.add(sid)
        thread = threading.Thread(target=worker, daemon=True, name="kolibri-"+sid)
        try:
            thread.start()
        except BaseException:
            manager.active.remove(sid)
            raise


def check_update(manager, record):
    """Metadata-only check, bound to the installed exact scope and manifest."""
    if manager is None:
        return None
    sid = record["source_id"]
    artifact = manager.read(sid).get("installed")
    if not artifact:
        return None
    old = manager._selection(artifact)
    refreshed = refresh(manager, record)
    item = refreshed["channel"]
    result = {"installed_edition": str(old.version)}
    if item["version"] > old.version:
        try:
            selected, review = selection(manager, record, list(old.nodes), old.allow_full)
            if review["missing_license_count"]:
                raise ValueError("Resource license metadata incomplete")
            result.update(state="update_available", candidate_edition=str(selected.version),
                candidate_sha256=review["metadata_sha256"], candidate_bytes=review["total_bytes"],
                candidate_url=native.metadata_url(old.channel_id, selected.version, base_url=selected.base_url),
                evidence="Publisher channel has a newer version for the installed scope.")
        except (OSError, ValueError):
            result.update(state="unable_to_determine", evidence="Installed topic scope could not be preserved.")
    else:
        result.update(state="up_to_date" if item["version"] == old.version else "older_candidate",
                      evidence="Compared the current official channel version.")
    update_check_store.save(sid, artifact["manifest_sha256"], result)
    return result
