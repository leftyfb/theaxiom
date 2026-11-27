#!/usr/bin/env python3
"""Offline library admin API and trusted-LAN development server."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from itertools import groupby
import importlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, time as wall_time, timedelta
from urllib.parse import parse_qs, urlsplit, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

storage_settings = importlib.import_module(
    f"{__package__}.storage_settings" if __package__ else "storage_settings"
)

acquisition_queue = importlib.import_module(
    f"{__package__}.acquisition_queue" if __package__ else "theaxiom.acquisition_queue"
)
storage = importlib.import_module(f"{__package__}.storage" if __package__ else "theaxiom.storage")
catalog_store = importlib.import_module(f"{__package__}.catalog_store" if __package__ else "theaxiom.catalog_store")

security = importlib.import_module(f"{__package__}.admin_security" if __package__ else "theaxiom.admin_security")
admin_sessions = importlib.import_module(f"{__package__}.admin_sessions" if __package__ else "theaxiom.admin_sessions")
publication = importlib.import_module(f"{__package__}.publication" if __package__ else "theaxiom.publication")
pdf_source = importlib.import_module(f"{__package__}.pdf_source" if __package__ else "theaxiom.pdf_source")
epub_source = importlib.import_module(f"{__package__}.epub_source" if __package__ else "theaxiom.epub_source")
pdf_language = importlib.import_module(f"{__package__}.pdf_language" if __package__ else "theaxiom.pdf_language")
pdf_transport = importlib.import_module(f"{__package__}.pdf_transport" if __package__ else "theaxiom.pdf_transport")
document_origin_evidence = importlib.import_module(f"{__package__}.document_origin_evidence" if __package__ else "theaxiom.document_origin_evidence")
document_update_discovery = importlib.import_module(f"{__package__}.document_update_discovery" if __package__ else "theaxiom.document_update_discovery")
zim_jobs = importlib.import_module(f"{__package__}.zim_jobs" if __package__ else "theaxiom.zim_jobs")
pmtiles_source = importlib.import_module(f"{__package__ or 'theaxiom'}.pmtiles_source")
zim_source = importlib.import_module(f"{__package__}.zim_source" if __package__ else "theaxiom.zim_source")
zim_catalog = importlib.import_module(f"{__package__}.zim_catalog" if __package__ else "theaxiom.zim_catalog")
zim_refresh = importlib.import_module(f"{__package__}.zim_refresh" if __package__ else "theaxiom.zim_refresh")
ENDPOINT_TEST_LOCK = threading.Lock()
ENDPOINT_TEST_NEXT = 0.0
update_discovery_config = importlib.import_module(f"{__package__}.update_discovery_config" if __package__ else "theaxiom.update_discovery_config")
update_diagnostics = importlib.import_module(f"{__package__}.update_diagnostics" if __package__ else "theaxiom.update_diagnostics")
update_check_store = importlib.import_module(f"{__package__}.update_check_store" if __package__ else "theaxiom.update_check_store")
kiwix_update_discovery = importlib.import_module(f"{__package__}.kiwix_update_discovery" if __package__ else "theaxiom.kiwix_update_discovery")
kiwix_update_scheduler = importlib.import_module(f"{__package__ or 'theaxiom'}.kiwix_update_scheduler")
update_schedule = importlib.import_module(f"{__package__ or 'theaxiom'}.update_schedule")
library_search = importlib.import_module(f"{__package__}.search" if __package__ else "theaxiom.search")
library_selection = importlib.import_module(f"{__package__}.library_selection" if __package__ else "theaxiom.library_selection")
local_transfer = importlib.import_module(f"{__package__}.local_transfer" if __package__ else "theaxiom.local_transfer")
upstream_catalog = importlib.import_module(f"{__package__}.upstream_catalog" if __package__ else "theaxiom.upstream_catalog")
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080
ADMIN_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
PUBLIC_ICON_PATHS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}
PUBLIC_ICON_DIR = ADMIN_TEMPLATE_DIR
ADMIN_SESSIONS = admin_sessions.Sessions()
PDFJS_ROOT = Path("/opt/pdfjs/current")
MAP_ASSET_PATHS = {"/api/map-assets/" + name: mime for name, mime in {
    "maplibre-gl.mjs": "text/javascript", "maplibre-gl-worker.mjs": "text/javascript",
    "maplibre-gl-shared.mjs": "text/javascript", "pmtiles.js": "text/javascript",
    "maplibre-gl.css": "text/css", "style.json": "application/json",
    "map-reader.mjs": "text/javascript",
}.items()}
def _map_asset_path(path):
    try:
        location = urlsplit(path)
    except ValueError:
        return None
    if (location.path in MAP_ASSET_PATHS and location.query in {"", "v=6.19.0"}
            and not location.fragment and not location.netloc and not location.scheme):
        return location.path
    return None


PDFJS_PREFIX = "/api/pdfjs/"
ZIM_READER_ROUTE = re.compile(r'/read/zim/([a-z0-9-]{1,63})\Z')
PDFJS_ASSET = re.compile(r"/api/pdfjs/(?:build|web|cmaps|standard_fonts|wasm|iccs|image_decoders|locale)/[A-Za-z0-9_./-]+\Z")

ACTION_LABELS = {"download": "Download", "redownload": "Re-download", "update": "Update"}
def _release_revision() -> str | None:
    """Return the immutable source revision loaded by this process, if known."""
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None


FORMAT_ADAPTERS = {"zim": "kiwix", "pmtiles": "pmtiles", "pdf": "pdf", "epub": "epub"}


def select_lifecycle(source_id, configuration):
    """Select by format first; candidate configuration never creates trust."""
    format_name = configuration.get("format")
    adapter = FORMAT_ADAPTERS.get(format_name) if isinstance(format_name, str) else None
    if adapter == "epub":
        artifact = None
        try:
            artifact = epub_source.pin(source_id, configuration, require_rights=False)
        except ValueError:
            pass
        return dict(adapter=adapter, configuration_valid=artifact is not None, artifact=artifact,
                    pdf_download=pdf_source.valid_source_url(configuration.get("source_url", "")),
                    acquisition_reason="Upload an EPUB or download from a direct HTTPS URL. Publication is separate.", actions={})
    if adapter == "pdf":
        artifact = None
        try:
            artifact = pdf_source.pin(source_id, configuration, require_rights=False)
            reason = "Download from the configured HTTPS URL or upload a PDF from your computer."
        except ValueError as exc:
            reason = str(exc)
        return dict(adapter=adapter, configuration_valid=artifact is not None, artifact=artifact,
                    pdf_download=pdf_source.valid_source_url(configuration.get("source_url", "")),
                    acquisition_reason=reason, actions={})
    if adapter in {"kiwix", "pmtiles"}:
        try:
            zim_source.candidate(configuration)
        except ValueError as exc:
            return dict(adapter=adapter, configuration_valid=False, artifact=None,
                        acquisition_reason=str(exc), actions={}, zim_download=False)
        return dict(adapter=adapter, configuration_valid=True, artifact=None,
                    acquisition_reason="", actions={}, zim_download=True)
    return dict(adapter=None, configuration_valid=False, artifact=None,
                acquisition_reason="No lifecycle adapter supports this format.", actions={})


RELEASE_REVISION = _release_revision()


def health_response() -> dict[str, Any]:
    """Return liveness and immutable source revision loaded by this process."""
    return {
        "status": "ok",
        "service": "offline-library-admin-api",
        "revision": RELEASE_REVISION,
    }


# Local development defaults follow the XDG user data location. Deployments can
# override both values explicitly without depending on this source repository.
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SOURCE_CATALOG_PATH = Path(os.environ.get("THEAXIOM_SOURCE_CATALOG", Path(storage_settings.SETTINGS_PATH or CONFIG_HOME / "theaxiom/storage.json").with_name("sources.json")))
SOURCE_CATALOG = catalog_store.CatalogStore(SOURCE_CATALOG_PATH)
# Keep process handles so completed jobs can be distinguished from active ones.
_active_downloads: dict[str, subprocess.Popen[bytes]] = {}
_jobs: dict[str, dict[str, Any]] = {}
_active_downloads_lock = threading.Lock()
MAX_RETAINED_JOBS = 100
STORAGE_PROBE = storage.StorageProbe()


def source_busy(snapshot, source_id):
    if snapshot["stale"] or any(
        job["source_id"] == source_id and (job.get("active") is not False
        or job.get("state") not in ("completed", "not_started", "unavailable"))
        for job in snapshot["acquisitions"]
    ):
        return True
    with _active_downloads_lock:
        return any(job["source_id"] == source_id and job["process"].poll() is None
                   for job in _jobs.values())


def job_status_response(job_id: str) -> dict[str, Any] | None:
    """Return a bounded status record for a retained acquisition job."""
    with _active_downloads_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        process = job["process"]
        return {
            "job_id": job_id,
            "source_id": job["source_id"],
            "action": job["action"],
            "status": "pending" if process.poll() is None else (
                "completed" if process.returncode == 0 else "failed"
            ),
            "returncode": process.returncode,
        }


def _public_acquisition(job):
    """Only expose recorded outcomes and timestamps from the attempt journal."""
    if job is None:
        return None
    job = {key: value for key, value in job.items() if key not in {"promotion_intent", "adopted_from_legacy", "installed_provenance"}}
    if "checksum_sampled_at" in job:
        sample = zim_jobs.current_checksum(job)
        job.pop("checksum_sampled_at")
        for field in zim_jobs.CHECKSUM_FIELDS:
            job.pop(field, None)
        job.update(sample)
    if "attempt_history" not in job:
        return job
    history = job["attempt_history"]
    if not isinstance(history, list):
        history = []
    outcomes = {"completed", "failed", "paused", "cancelled", "interrupted"}
    attempts = []
    for entry in history[-zim_jobs.HISTORY_LIMIT:]:
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        finished = entry.get("finished_at")
        if (not isinstance(state, str) or state not in outcomes
                or type(finished) not in (int, float) or not 0 < finished < 4102444800):
            continue
        attempts.append({"state": state, "finished_at": finished})
    result = dict(job, attempt_history=attempts)
    if "retry_count" not in result:
        result["retry_count"] = sum(isinstance(entry, dict) and entry.get("operation") == "retry"
                                    for entry in history)
    result["last_successful_acquisition"] = max(
        (attempt["finished_at"] for attempt in attempts if attempt["state"] == "completed"),
        default=None,
    )
    return result


def _public_zim_replacement(candidate):
    """Expose only replacement UI fields, never internal rollback records."""
    if candidate is None:
        return None
    result = {key: candidate[key] for key in (
        "state", "downloaded_bytes", "expected_bytes", "error", "automatic_retry_count", "retry_delay_seconds"
    ) if key in candidate}
    if candidate["state"] in {"verified", "rolled_back"}:
        result["sha256"] = candidate["artifact"].get("sha256")
    return result | zim_jobs.current_checksum(candidate)


def _update_stage(source):
    if not source["installed_verified"]:
        return None
    check = source["update_check"] or {}
    replacement = source["replacement"] or {}
    if replacement.get("state") == "verified":
        return "publish"
    if (check.get("state") == "update_available" and check.get("candidate_bytes")
            and replacement.get("state") in (None, "activated")):
        return "download"
    return None


def _source_update_summary(sources):
    """Summarize cached metadata for verified sources on the admin landing page."""
    available = 0
    attention = 0
    for source in sources:
        discovery = source["discovery"]
        if not source["installed_verified"] or discovery["method"] == "manual":
            continue
        result = source["update_check"] or {}
        state = result.get("state")
        if state == "update_available":
            available += 1
        elif (state == "representation_changed" or
              (state in (None, "unable_to_determine")
               and (discovery["periodic"] or result.get("checked_at")))):
            attention += 1
    messages = []
    if available:
        messages.append("1 source has an update available" if available == 1
                        else f"{available} sources have updates available")
    if attention:
        messages.append("1 source needs update review" if attention == 1
                        else f"{attention} sources need update review")
    return " · ".join(messages)


def _preview_capacity(rows):
    eligible = [row for row in rows if (
        row["format"] in {"zim", "pmtiles"} and row["state"] != "unsupported"
        or row["format"] in {"pdf", "epub"} and row["state"] == "estimated")]
    staging = [row.get("staging_bytes") for row in eligible]
    if not staging:
        return {"state": "not_applicable"}
    try:
        settings = storage_settings.load_settings()
        sample = STORAGE_PROBE.status(settings, formats=tuple(sorted({row['format'] for row in eligible})))
    except (OSError, ValueError):
        return {"state": "unavailable"}
    if sample.get("pending"):
        return {"state": "pending"}
    free = sample.get("free_bytes")
    if (type(free) is not int or free < 0 or
            (sample.get("write_ready") is not True and sample.get("error") != "insufficient_capacity")):
        return {"state": "unavailable"}
    reserve = settings["minimum_free_bytes"]
    known_staging = sum(size for size in staging if size is not None)
    unknown_limits = [row.get("staging_limit_bytes") for row in eligible if row.get("staging_bytes") is None]
    upper_bound = (known_staging + sum(unknown_limits)
                   if unknown_limits and None not in unknown_limits else None)
    available = max(0, free - reserve)
    if free < reserve or known_staging > available:
        state = "insufficient"
    elif not unknown_limits:
        state = "fits"
    elif upper_bound is not None and upper_bound <= available:
        state = "fits_with_unknown_sizes"
    else:
        state = "uncertain"
    result = {"state": state, "free_bytes": free, "reserve_bytes": reserve,
              "available_bytes": available}
    if upper_bound is not None:
        result["staging_upper_bound_bytes"] = upper_bound
    return result


def _preview_document_acquisition(configuration, *, byte_limit):
    # Candidate metadata does not prove the document is present or verified.
    url = configuration.get("source_url", "")
    if not url:
        return {"state": "upload_required"}
    if not pdf_source.valid_source_url(url):
        return {"state": "invalid_download_url"}
    size = configuration.get("expected_size_bytes", "")
    if size and int(size) > byte_limit:
        return {"state": "over_size_limit"}
    requested = int(size) if size else None
    estimate = {"state": "estimated", "transfer_bytes": requested, "staging_bytes": requested,
                "installed_bytes": None, "peak_bytes": None}
    if requested is None:
        estimate["staging_limit_bytes"] = byte_limit
    return estimate


_DOCUMENT_RETRIES = {}
_DOCUMENT_RETRIES_LOCK = threading.Lock()


def _acquire_document(sid, record, config, revision, is_epub, fields, *, adding=False,
                      auto_title=False, stop=None):
    """Acquire and commit a document using the caller's publication lock."""
    stopping = stop or (lambda: False)
    stop_options = {"stop": stop} if stop else {}
    url = config.get("source_url", "")
    if not pdf_source.valid_source_url(url):
        raise ValueError("A direct HTTPS PDF URL is required")
    size = config.get("expected_size_bytes") or None
    digest = config.get("sha256") or None
    if stopping():
        raise acquisition_queue.Cancelled()
    settings = storage_settings.load_settings()

    def transfer():
        with _DOCUMENT_RETRIES_LOCK:
            if sid in _DOCUMENT_RETRIES:
                _DOCUMENT_RETRIES[sid]["state"] = "downloading"
                _DOCUMENT_RETRIES[sid].pop("retry_delay_seconds", None)
        if storage_settings.load_settings() != settings:
            raise ValueError("Document storage settings changed during acquisition")
        if is_epub:
            with pdf_transport.open_pdf(url, format_name="epub") as stream:
                observed, extracted = epub_source.install_stream(sid, stream,
                    expected_size=int(size) if size else None, expected_sha256=digest, **stop_options)
                return observed, extracted, getattr(stream, "etag", None)
        observed, etag = pdf_transport.download(
            sid, url, expected_size=int(size) if size else None,
            expected_sha256=digest, capture_validator=True, **stop_options)
        return observed, {}, etag

    def on_retry(count, delay, _error):
        with _DOCUMENT_RETRIES_LOCK:
            _DOCUMENT_RETRIES[sid] = dict(state="retrying", automatic_retry_count=count,
                                          retry_delay_seconds=delay)

    try:
        observed, extracted, installed_etag = pdf_transport.run_with_download_retries(
            transfer, stopped=stop, on_retry=on_retry)
    finally:
        with _DOCUMENT_RETRIES_LOCK:
            _DOCUMENT_RETRIES.pop(sid, None)
    if not is_epub:
        if not record["profile"].get("language"):
            language = pdf_language.installed_language(observed)
            if language:
                extracted["language"] = language
    provenance = config.get("provenance", "").strip()
    if not provenance:
        provenance = "Downloaded via configured HTTPS URL; SHA-256 calculated locally, not independently publisher-verified."
    elif not digest:
        suffix = " SHA-256 calculated locally, not independently publisher-verified."
        provenance = provenance[:catalog_store.MAX_FIELD - len(suffix)] + suffix
    fields.update(expected_size_bytes=str(observed["bytes"]), sha256=observed["sha256"],
                  provenance=provenance)
    profile = fields if adding else record["profile"]
    for key in ("author", "publisher", "language"):
        if not profile.get(key) and extracted.get(key):
            fields[key] = extracted[key]
    if adding and auto_title and extracted.get("title"):
        fields["title"] = extracted["title"]
    updated = SOURCE_CATALOG.mutate("add" if adding else "edit", sid, fields, revision)
    # Preserve only a strong ETag captured from the actual successful GET,
    # and only after the verified artifact and source catalog both commit.
    # This auxiliary evidence is advisory; failures must never convert an
    # already installed archive into a failed acquisition.
    if installed_etag:
        try:
            document_origin_evidence.save(
                sid, "epub" if is_epub else "pdf", url,
                observed["sha256"], installed_etag)
        except (OSError, ValueError):
            pass
    return {"accepted": True, "installed_verified": True, "revision": updated["revision"]}


def _batch_job(record, published):
    sid = record["source_id"]
    config = record["configuration"]
    kind = config["format"]
    size = config.get("expected_size_bytes")
    if kind in {"pdf", "epub"}:
        limit = pdf_source.MAX_BYTES if kind == "pdf" else epub_source.MAX_BYTES
        if (published.get(sid) or not pdf_source.valid_source_url(config.get("source_url", ""))
                or size and int(size) > limit):
            raise ValueError("Document must be unpublished and have a supported download URL and size")
        adapter = pdf_source if kind == "pdf" else epub_source
        try:
            pin = adapter.pin(sid, config, require_rights=False)
        except ValueError:
            pin = None
        if pin and adapter.ready(pin, verify=False):
            raise ValueError("Document is already installed")
        operation, reservation = kind, int(size) if size else limit
    elif kind in {"zim", "pmtiles"}:
        identity = zim_jobs.identity(config)
        if not size:
            raise ValueError("A ZIM download needs a known size to reserve space")
        current = zim_jobs.read(sid)
        candidate = zim_jobs.read_candidate(sid)
        if candidate and candidate["state"] == "prepared" and candidate["configuration"] == identity:
            operation = "replacement"
        elif current is None and not published.get(sid) and candidate is None:
            operation = "zim"
        else:
            raise ValueError("Resolve the existing ZIM acquisition or prepare a replacement first")
        reservation = int(size)
    else:
        raise ValueError("Unsupported batch source format")
    return dict(source_id=sid, title=record["profile"]["title"], operation=operation,
                configuration=config, bytes=reservation)


def _batch_available(settings, formats):
    sample = storage.inspect_storage(settings, formats=tuple(sorted(formats)))
    if not sample["write_ready"]:
        raise ValueError("Configured storage is not ready for acquisition")
    return max(0, sample["free_bytes"] - settings["minimum_free_bytes"])


def _reviewed_update_fields(update, reviewed_digest):
    """Resolve a reviewed publisher digest to its actual algorithm."""
    if (update.get("state") != "update_available" or update.get("stale") or not update.get("candidate_bytes")
            or (update.get("candidate_sha256") or update.get("candidate_sha1") or update.get("candidate_md5")) != reviewed_digest):
        raise ValueError("Update evidence changed; check for updates again")
    fields = {"source_url": update["candidate_url"], "sha256": update.get("candidate_sha256", ""),
              "expected_size_bytes": str(update["candidate_bytes"]), "edition": update["candidate_edition"]}
    if update.get("candidate_sha1"):
        fields["publisher_sha1"] = update["candidate_sha1"]
    if update.get("candidate_md5"):
        fields["publisher_md5"] = update["candidate_md5"]
    return fields


def _publisher_update_job(record, reviewed_digest, operation, *, rollback_retry=None) -> dict[str, Any]:
    sid = record["source_id"]
    config = record["configuration"]
    if config["format"] not in {"zim", "pmtiles"}:
        raise ValueError("This source has no verifiable replacement workflow")
    current = zim_jobs.candidate_status(sid)
    reservation = 0
    if operation == "publish-update":
        if not current or current["state"] != "verified" or current["artifact"]["sha256"] != reviewed_digest:
            raise ValueError("The verified candidate changed; review it again")
        payload = config
    else:
        catalog_store.CatalogStore._validate_id(sid + "-candidate")
        if update_discovery_config.settings(config)["method"] not in {"kiwix", "archive", "builds"}:
            raise ValueError("Source does not use a versioned archive update method")
        update = kiwix_update_discovery.cached_checked_source(sid)
        fields = _reviewed_update_fields(update, reviewed_digest)
        if config.get("update_method") == "archive" and config.get("update_endpoint"):
            fields["update_endpoint"] = update_discovery_config.settings(
                config | fields | {"update_endpoint": ""})["endpoint"]
        live = zim_jobs.read(sid)
        if not live or live.get("state") != "completed" or not live.get("artifact"):
            raise ValueError("No installed archive to replace")
        payload = {"original": config, "fields": fields, "base": live["artifact"]}
        reservation = update["candidate_bytes"]
        if rollback_retry:
            if (_matching_rollback(record, current, reviewed_digest) != rollback_retry
                    or zim_jobs.identity(_queued_update_configuration(payload)) != current["configuration"]):
                raise ValueError("Cached publisher evidence no longer matches the rolled-back edition")
            payload["rollback_retry"] = rollback_retry
            reservation = 0  # Reuse the retained, verified artifact; no acquisition.
        elif current and current["state"] != "activated":
            if not (operation == "automatic-update" and _restored_rollback(record, current, live)
                    and reviewed_digest not in {_rollback_digest(current), current["artifact"]["sha256"]}
                    and fields["source_url"] != current["configuration"]["source_url"]):
                raise ValueError("Resolve the existing replacement on its source card")
            payload["supersede_rollback"] = zim_jobs.rollback_token(current)
    return dict(source_id=sid, title=record["profile"]["title"], operation=operation,
                configuration=payload, bytes=reservation)


def _queued_update_configuration(payload):
    return payload["original"] | {key: value for key, value in payload["fields"].items()
                                  if key in catalog_store.CONFIG_FIELDS}


def _prepare_queued_update(sid, record, revision, payload, *, automatic_job=None):
    original, fields = payload["original"], payload["fields"]
    config = _queued_update_configuration(payload)
    if record["configuration"] not in (original, config):
        raise ValueError("Source configuration changed after queuing")
    current = zim_jobs.candidate_status(sid)
    if (current and current["configuration"] == zim_jobs.identity(config)
            and current["base_artifact"] == payload["base"]):
        if automatic_job is not None and current.get("automatic_job") != automatic_job:
            raise ValueError("Replacement is not owned by this automatic attempt")
        if record["configuration"] == original:
            SOURCE_CATALOG.mutate("edit", sid, fields, revision)
        return config, current
    live = zim_jobs.read(sid)
    if not live or live.get("artifact") != payload["base"]:
        raise ValueError("Installed archive changed after queuing")
    supersede = payload.get("supersede_rollback")
    if current and current["state"] != "activated" and not supersede:
        raise ValueError("Another replacement already exists")
    if current and current["configuration"] == zim_jobs.identity(config):
        raise ValueError("The reviewed edition is already installed")
    with zim_jobs.LOCK:
        # Serialize the base check and candidate preparation with manual ZIM actions.
        candidate = zim_jobs.prepare_candidate(sid, config, replace_activated=True, automatic_job=automatic_job,
                                               supersede_rollback=supersede)
        SOURCE_CATALOG.mutate("edit", sid, fields, revision)
    return config, candidate


def _publish_queued_update(sid, config, recovering):
    candidate = zim_jobs.candidate_status(sid)
    if not candidate or candidate["configuration"] != zim_jobs.identity(config):
        raise ValueError("The verified candidate changed; review it again")
    enabled = publication.load().get(sid, False)
    if candidate["state"] != "activated" or not recovering:
        zim_jobs.activate_candidate(sid, config)
    else:
        zim_jobs.recover_candidate_transition(sid)
    return enabled


def _automatic_digest(update):
    return update.get("candidate_sha256") or update.get("candidate_sha1") or update.get("candidate_md5")


def _rollback_digest(candidate):
    target = candidate["configuration"]
    return target.get("sha256") or target.get("publisher_sha1") or target.get("publisher_md5")


def _restored_rollback(record, candidate, live):
    # Baseline rollback restored the serving job but left catalog intent on
    # the replacement. Accept either known identity, never an unrelated edit.
    return (candidate and candidate["state"] == "rolled_back" and live
            and live == candidate["previous_job"] and live.get("artifact") == candidate["base_artifact"]
            and live.get("state") == "completed" and live.get("verified") is True
            and zim_jobs.identity(record["configuration"]) in (candidate["configuration"], live["configuration"]))


def _matching_rollback(record, candidate, digest):
    live = zim_jobs.read(record["source_id"])
    if (_restored_rollback(record, candidate, live) and candidate.get("automatic_job")
            and not candidate.get("error")  # Reader-recovery rollback records carry a reason.
            and digest == _rollback_digest(candidate)):
        return candidate["automatic_job"], zim_jobs.rollback_token(candidate)
    return None


def _automatic_status(config, paused, now, attempt):
    policy, start, _ = update_discovery_config.policy(config)
    if policy not in update_discovery_config.UNATTENDED:
        return {"policy": policy, "next": "", "attempt": ""}
    if paused:
        upcoming = "Paused for new downloads"
    elif update_discovery_config.window_open(config, now):
        upcoming = "Next queue poll if eligible"
    else:
        opening = datetime.combine(now.date(), wall_time.fromisoformat(start), now.tzinfo)
        if opening <= now:
            opening += timedelta(days=1)
        upcoming = f"Next window: {opening:%Y-%m-%d %H:%M} (server local)"
    return {"policy": policy, "next": upcoming, "attempt": attempt}


def _poll_automatic_updates():
    """Use cached metadata only; offer at most one job per bounded poll."""
    with publication.LOCK, zim_jobs.LOCK:
        if update_schedule.automatic_paused():
            return
        now = datetime.now(update_schedule.timezone())
        eligible = [record for record in SOURCE_CATALOG.get()["sources"]
                    if update_discovery_config.window_open(record["configuration"], now)]
        if not eligible:
            return
        pending = {job["source_id"] for job in acquisition_queue.status() if job["state"] in acquisition_queue.PENDING}
        attempted = {(row["source_id"], row["digest"]): row for row in acquisition_queue.automatic_attempts()}
        for record in eligible:
            sid, config = record["source_id"], record["configuration"]
            if sid in pending:
                continue
            try:
                live = zim_jobs.read(sid)
                if not live or live.get("state") != "completed":
                    continue
                evidence = kiwix_update_discovery.cached_checked_source(sid)
                digest = _automatic_digest(evidence)
                if evidence.get("state") != "update_available" or not digest:
                    continue
                previous = attempted.get((sid, digest))
                candidate = zim_jobs.read_candidate(sid)
                rollback_retry = None
                if previous:
                    rollback_retry = _matching_rollback(record, candidate, digest)
                    if (previous["state"] != "completed" or not rollback_retry
                            or previous["job_id"] != rollback_retry[0]):
                        continue
                elif not zim_jobs.matches(live, config) and not _restored_rollback(record, candidate, live):
                    continue
                job = _publisher_update_job(record, digest, "automatic-update", rollback_retry=rollback_retry)
                job["configuration"]["digest"] = digest
                settings = storage_settings.load_settings()
                available = _batch_available(settings, {config["format"]}) if not rollback_retry else None
                acquisition_queue.enqueue([job], available, settings)
                return
            except (ValueError, OSError):
                # Another candidate, insufficient space, or a busy archive worker.
                continue


def _execute_automatic_update(job, payload, stopping, shutdown):
    sid, job_id = job["source_id"], job["id"]
    original = payload["original"]
    config = _queued_update_configuration(payload)
    kind = config["format"]
    # Legacy queued jobs still have to pass current policy and rollback checks.
    rollback_retry = payload.get("rollback_retry") or payload.get("rearm")

    def same_configuration(left, right):
        if not rollback_retry:
            return left == right
        policy_fields = catalog_store.UPDATE_POLICY_FIELDS
        return ({key: value for key, value in left.items() if key not in policy_fields}
                == {key: value for key, value in right.items() if key not in policy_fields})

    while not shutdown.is_set():
        with publication.LOCK:
            catalog = SOURCE_CATALOG.get()
            record = next((record for record in catalog["sources"] if record["source_id"] == sid), None)
            candidate = zim_jobs.candidate_status(sid)
            owned = (candidate and candidate.get("automatic_job") == job_id
                     and candidate["configuration"] == zim_jobs.identity(config)
                     and candidate["base_artifact"] == payload["base"])
            if owned and candidate["state"] in zim_jobs.ACTIVE:
                if stopping() and candidate["state"] not in {"cancelling", "publishing", "pausing"}:
                    try:
                        zim_jobs.cancel_candidate_acquisition(sid, config)
                    except ValueError:
                        pass  # Transfer crossed into a terminal state.
            else:
                if stopping():
                    raise acquisition_queue.Cancelled()
                if not record or not any(same_configuration(record["configuration"], expected)
                                         for expected in (original, config)):
                    return "interrupted", "Source configuration or update policy changed; review manually"
                current_config = record["configuration"]
                if owned and candidate["state"] in {"verified", "activated"}:
                    if not same_configuration(current_config, config):
                        return "interrupted", "Source configuration changed; review manually"
                    try:
                        published = _publish_queued_update(sid, config, job["state"] == "running")
                    except BlockingIOError:
                        return "running", ""
                    break
                if owned and candidate["state"] != "prepared":
                    state = candidate["state"]
                    return state if state in {"failed", "cancelled"} else "interrupted", "Review replacement on the source card"
                if (update_schedule.automatic_paused() or not update_discovery_config.window_open(
                        current_config, datetime.now(update_schedule.timezone()))):
                    return "deferred", "Waiting for the maintenance window or automatic updates to resume"
                settings = storage_settings.load_settings()
                if json.loads(job["storage"]) != settings:
                    return "interrupted", "Storage settings changed; review manually"
                if rollback_retry and (_matching_rollback(record, candidate, payload["digest"]) != tuple(rollback_retry)
                                       or candidate["base_artifact"] != payload["base"]):
                    raise ValueError("The rollback changed; review manually")
                evidence = kiwix_update_discovery.cached_checked_source(sid)
                if rollback_retry and (evidence.get("stale") or evidence.get("state") == "unable_to_determine"):
                    return "deferred", "Waiting for current publisher evidence"
                if _reviewed_update_fields(evidence, payload["digest"]) != {
                        key: value for key, value in payload["fields"].items() if key != "update_endpoint"}:
                    raise ValueError("Update evidence changed; review manually")

                if not rollback_retry and _batch_available(settings, {kind}) < job["bytes"]:
                    return "deferred", "Waiting for free storage"
                try:
                    if rollback_retry:
                        if not same_configuration(current_config, config):
                            SOURCE_CATALOG.mutate("edit", sid, payload["fields"], catalog["revision"])
                        if stopping():
                            raise acquisition_queue.Cancelled()
                        if shutdown.is_set():
                            return "running", ""
                        if (update_schedule.automatic_paused() or not update_discovery_config.window_open(
                                current_config, datetime.now(update_schedule.timezone()))):
                            return "deferred", "Waiting for the maintenance window or automatic updates to resume"
                        published = publication.load().get(sid, False)
                        zim_jobs.activate_candidate(sid, config, rollback_retry=(*rollback_retry, job_id))
                        break
                    _prepare_queued_update(sid, record, catalog["revision"], payload, automatic_job=job_id)
                    if (update_schedule.automatic_paused() or not update_discovery_config.window_open(
                            original, datetime.now(update_schedule.timezone()))):
                        return "deferred", "Waiting for the maintenance window or automatic updates to resume"
                    if stopping():
                        raise acquisition_queue.Cancelled()
                    if shutdown.is_set():
                        return "running", ""
                    zim_jobs.start_candidate_acquisition(sid, config, automatic_job=job_id)
                except BlockingIOError:
                    return "deferred", "Waiting for the archive worker"
        shutdown.wait(1)
    else:
        return "running", ""
    if kind == "zim" and published and not zim_refresh.reconcile_activation(
            lambda: SOURCE_CATALOG.get()["sources"], source_id=sid, configuration=config):
        return "failed", "Reader could not serve the update; review the replacement status"
    return "completed", "Update activated; publication setting preserved"


def _execute_batch_job(job, stopping, shutdown):
    sid, operation = job["source_id"], job["operation"]
    recovering = job["state"] == "running"
    config = json.loads(job["configuration"])
    if operation == "automatic-update":
        return _execute_automatic_update(job, config, stopping, shutdown)
    kind = config["original"]["format"] if operation == "download-update" else config["format"]

    def stop_document():
        return stopping() or shutdown.is_set()

    with publication.LOCK:
        settings = storage_settings.load_settings()
        if settings != json.loads(job["storage"]):
            raise ValueError("Storage settings changed; review and queue again")
        catalog = SOURCE_CATALOG.get()
        record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
        if not record or (operation != "download-update" and record["configuration"] != config):
            raise ValueError("Source configuration changed; review the catalog before retrying")
        if operation == "publish-update":
            if stopping():
                raise acquisition_queue.Cancelled()
            try:
                is_published = _publish_queued_update(sid, config, recovering)
            except BlockingIOError:
                return "running" if recovering else "queued", ""
        elif operation in {"pdf", "epub"}:
            if stopping():
                raise acquisition_queue.Cancelled()
            if recovering:
                return "interrupted", "Document transfer stopped; review the source before queuing again"
            _batch_job(record, publication.load())
            if _batch_available(settings, {kind}) < job["bytes"]:
                raise ValueError("Free space fell below the reserved download size")
            if shutdown.is_set():
                return "running", ""
            try:
                _acquire_document(sid, record, config, catalog["revision"], operation == "epub",
                                  {}, stop=stop_document)
            except acquisition_queue.Cancelled:
                if shutdown.is_set() and not stopping():
                    return "running", ""
                raise
            return "completed", ""
        if operation != "publish-update":
            replacement = operation in {"replacement", "download-update"}
            inspect = zim_jobs.candidate_status if replacement else zim_jobs.status
            if operation == "download-update":
                if stopping() and not recovering:
                    raise acquisition_queue.Cancelled()
                try:
                    config, current = _prepare_queued_update(sid, record, catalog["revision"], config)
                except BlockingIOError:
                    return "running" if recovering else "queued", ""
            else:
                current = inspect(sid) if recovering else None
            finished = "verified" if replacement else "completed"
            if current and current["configuration"] != zim_jobs.identity(config):
                raise ValueError("ZIM job configuration changed")
            if current and current["state"] == finished:
                return "completed", "Replacement ready for activation" if replacement else ""
            if stopping() and not (current and current["state"] in zim_jobs.ACTIVE):
                raise acquisition_queue.Cancelled()
            if not replacement and publication.load().get(sid):
                raise ValueError("Source was published after the download was queued")
            if not recovering and operation != "download-update":
                _batch_job(record, publication.load())
            if not current or current["state"] not in zim_jobs.ACTIVE:
                if current and current["state"] not in {"prepared", "paused", "interrupted"}:
                    return current["state"] if current["state"] in {"failed", "cancelled"} else "interrupted", "Review the existing ZIM job"
                try:
                    with zim_jobs.worker_lock():
                        pass
                except BlockingIOError:
                    return "running" if recovering else "queued", ""
                staging_id = sid + "-candidate" if replacement else sid
                retained = zim_jobs.staged_size(staging_id) if current else 0
                if _batch_available(settings, {kind}) < max(0, job["bytes"] - retained):
                    raise ValueError("Free space fell below the reserved download size")
                try:
                    if replacement:
                        zim_jobs.start_candidate_acquisition(sid, config)
                    else:
                        zim_jobs.start(sid, config, resume=bool(retained),
                                       restart=current is not None and not retained)
                except BlockingIOError:
                    return "running" if recovering else "queued", ""
    if operation == "publish-update":
        if kind == "zim" and is_published and not zim_refresh.reconcile_activation(
                lambda: SOURCE_CATALOG.get()["sources"], source_id=sid, configuration=config):
            return "failed", "Reader could not serve the update; review the replacement status"
        return "completed", "Update activated; publication setting preserved"
    while not shutdown.is_set():
        current = inspect(sid)
        if not current:
            raise ValueError("ZIM job record unavailable")
        if current["state"] not in zim_jobs.ACTIVE:
            if current["state"] == finished:
                return "completed", "Replacement ready for activation" if replacement else ""
            state = current["state"] if current["state"] in {"failed", "cancelled"} else "interrupted"
            return state, current.get("error", current.get("detail", ""))[:300]
        if stopping() and current["state"] not in {"cancelling", "publishing", "pausing"}:
            try:
                if replacement:
                    zim_jobs.cancel_candidate_acquisition(sid, config)
                else:
                    zim_jobs.control(sid, config, "cancel-keep")
            except ValueError:
                # Transfer may have reached a terminal state before cancellation.
                pass
        shutdown.wait(1)
    return "running", ""


class AdminAPIHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for the admin API."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _guard(self, *, post: bool = False) -> bool:
        """Strict host, Basic/client or browser session, origin and CSRF gate."""
        import hmac
        config = getattr(self.server, "axiom_config", None)
        if config is None:
            host, port = self.server.server_address[:2]
            config = security.ServerConfig(host, port, frozenset({
                f"{host}:{port}", f"localhost:{port}"
            }))
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not (security.valid_public_host(hosts[0]) if config.proxied else hosts[0].lower() in config.allowed_hosts):
            self._send_json(400, {"error": "invalid host"})
            return False
        # Reject cross-origin browser writes before any authentication
        # negotiation: a 403 is unambiguous and never triggers a Basic modal.
        if post and self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self._send_json(403, {"error": "cross-origin request rejected"})
            return False
        public_paths = set(PUBLIC_ICON_PATHS) | {
            "/", "/index.html", "/health", "/health/", "/api/pdf-reader.mjs"}
        if not post and (self.path in public_paths or self.path == "/admin/login"
                         or self.path.split("?", 1)[0] in {"/", "/index.html", "/search"}
                         or pdf_source.ROUTE.fullmatch(self.path) or epub_source.ROUTE.fullmatch(self.path)
                         or pmtiles_source.ROUTE.fullmatch(self.path) or _map_asset_path(self.path)
                         or ZIM_READER_ROUTE.fullmatch(self.path) or PDFJS_ASSET.fullmatch(self.path)
                         or self.path == "/internal/publication/zim"):
            return True

        # The login submission is public, but still passes all content-type,
        # size, host, fetch-site and mandatory same-origin checks below.
        form_login = post and self.path == "/admin/login"
        login = form_login or (post and self.path == "/api/admin/login")
        auth = self.headers.get_all("Authorization", [])
        local_hosts = {f"{config.host}:{config.port}", f"localhost:{config.port}"}
        if config.port == 80:
            local_hosts.update({config.host, "localhost"})
        self._session_csrf = None
        self._session_token = None
        # Browsers must use revocable sessions, not cached HTTP Basic
        # credentials that cannot be cleared by the application's Sign out.
        # Preserve Basic for explicit nonbrowser clients and deploy probes.
        browser_admin_navigation = (not post and self.path.rstrip("/") in {
            "/admin", "/admin/sources", "/admin/settings", "/admin/groupings"
        } and "text/html" in self.headers.get("Accept", "").lower())
        # Browser background fetches may omit Sec-Fetch-Mode (notably older
        # clients and some proxied requests). Other browser-only headers must
        # still prevent a Basic challenge after the session cookie expires.
        browser_request = (browser_admin_navigation or
                           self.headers.get("Sec-Fetch-Mode") in {
                               "navigate", "cors", "same-origin"
                           } or bool(self.headers.get("Sec-Fetch-Site"))
                           or bool(self.headers.get("Sec-Fetch-Dest"))
                           or bool(self.headers.get("Cookie"))
                           # Some programmatic Basic clients legitimately send
                           # Origin for CSRF validation. A bare Origin/Referer
                           # without browser fetch metadata is browser-like only
                           # when no explicit Authorization header is supplied.
                           or (not auth and (bool(self.headers.get("Origin"))
                                             or bool(self.headers.get("Referer")))))
        basic = ((config.store is None or not browser_request) and len(auth) <= 1 and
                 not (config.store is None and hosts[0].lower() not in local_hosts)
                 and config.authorized(auth[0] if auth else None))
        if not basic and (not auth or browser_request) and config.store is not None:
            token = admin_sessions.token_from_headers(self.headers)
            csrf = ADMIN_SESSIONS.authenticate(token, config.store)
            if csrf is not None:
                self._session_csrf, self._session_token = csrf, token
        if not login and not basic and self._session_csrf is None:
            if not post and self.path.rstrip("/") in {
                    "/admin", "/admin/sources", "/admin/settings", "/admin/groupings"}:
                # Real browser navigation advertises HTML; render the branded
                # login. Legacy curl/API probes still receive their expected
                # 401 Basic challenge, preserving fixed deploy preflight checks.
                accepts = self.headers.get_all("Accept", [])
                browser_navigation = (len(accepts) == 1 and any(
                    mime.strip().split(";", 1)[0].strip().lower() == "text/html"
                    for mime in accepts[0].split(",")
                ))
                if browser_navigation:
                    self.send_response(303)
                    self.send_header("Location", "/admin/login")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            # Browser fetches never receive a native Basic challenge; explicit
            # API clients can still discover/challenge the legacy Basic realm.
            if browser_request:
                self._send_json(401, {"error": "administrator sign in required"})
            else:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="The Axiom", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            return False
        if post:
            origins = self.headers.get_all("Origin", [])
            scheme = self.headers.get("X-Forwarded-Proto", "") if config.proxied else "http"
            if scheme not in {"http", "https"}:
                self._send_json(400, {"error": "invalid proxy scheme"})
                return False
            if (len(origins) > 1 or
                    (origins and origins[0] != scheme + "://" + hosts[0].lower()) or
                    self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
                self._send_json(403, {"error": "cross-origin request rejected"})
                return False
            if login or self._session_csrf is not None:
                if not origins:
                    self._send_json(403, {"error": "same-origin request required"})
                    return False
            # Native login has no session CSRF header and requires an exact Origin.
            if self._session_csrf is not None and not form_login:
                provided = self.headers.get_all("X-Axiom-CSRF", [])
                if (len(provided) != 1 or len(provided[0]) > 256 or
                        not hmac.compare_digest(provided[0], self._session_csrf)):
                    self._send_json(403, {"error": "invalid request token"})
                    return False
            upload = self.path == "/api/pdf-upload"
            formats = self.headers.get_all("X-Document-Format", []) if upload else []
            content_type = ("application/epub+zip" if formats == ["epub"] else "application/pdf") if upload else "application/json"
            if form_login:
                content_type = "application/x-www-form-urlencoded"
            if self.headers.get_content_type() != content_type:
                self._send_json(415, {"error": content_type + " required"})
                return False
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or
                    int(lengths[0]) > (pdf_source.MAX_BYTES if upload else 16384) or self.headers.get("Transfer-Encoding")):
                self._send_json(400, {"error": "invalid request length"})
                return False
        return True

    def setup(self) -> None:
        self.request.settimeout(15)
        super().setup()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - http.server convention
        if not self._guard():
            return
        if self.path == "/api/acquisition-batch":
            try:
                with _DOCUMENT_RETRIES_LOCK:
                    retries = {sid: dict(status) for sid, status in _DOCUMENT_RETRIES.items()}
                result = {"jobs": acquisition_queue.status()}
                if retries:
                    result["document_retries"] = retries
                self._send_json(200, result)
            except (OSError, sqlite3.Error):
                self._send_json(503, {"error": "Batch queue unavailable"})
            return
        if self.path == "/admin/login":
            self._send_admin_login()
            return
        if self.path in PUBLIC_ICON_PATHS:
            filename, mime = PUBLIC_ICON_PATHS[self.path]
            try:
                payload = (PUBLIC_ICON_DIR / filename).read_bytes()
            except OSError:
                self._send_json(404, {"error": "icon unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/pdf-reader.mjs":
            body = (ADMIN_TEMPLATE_DIR / "pdf-reader.mjs").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if PDFJS_ASSET.fullmatch(self.path):
            self._serve_pdfjs_asset()
            return
        if _map_asset_path(self.path):
            self._serve_map_asset()
            return
        map_route = pmtiles_source.ROUTE.fullmatch(self.path)
        if map_route:
            self._serve_pmtiles(*map_route.groups())
            return
        epub_route = epub_source.ROUTE.fullmatch(self.path)
        if epub_route:
            self._serve_epub(*epub_route.groups())
            return
        pdf_route = pdf_source.ROUTE.fullmatch(self.path)
        if pdf_route:
            self._serve_pdf(*pdf_route.groups())
            return
        if self.path.split("?", 1)[0] in {"/", "/index.html", "/search"}:
            search_page = self.path.split("?", 1)[0] == "/search"
            sources, groups, results, selected = [], [], [], {}
            query, error, code = "", "", 200
            search_source, search_number, start_base = "", 1, 1
            search_mode = ""
            try:
                if len(self.path) > 4096:
                    raise ValueError("Request too long")
                params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                                  max_num_fields=8 if search_page else 4, errors="strict")
                allowed = {"category", "collection", "tag", "q"}
                if search_page:
                    allowed.update({"source", "page", "base", "mode"})
                if set(params) - allowed or any(len(values) != 1 for values in params.values()):
                    raise ValueError("Invalid filters")
                query = library_search.validate_query(params.get("q", [""])[0])
                selected = {kind: params[kind][0] for kind in ("category", "collection", "tag")
                            if params.get(kind, [""])[0]}
                if any(not catalog_store.ID_RE.fullmatch(gid) for gid in selected.values()):
                    raise ValueError("Invalid filter ID")
                if search_page:
                    search_source = params.get("source", [""])[0]
                    search_mode = params.get("mode", [""])[0]
                    if (search_mode not in {"", "progressive", "source"}
                            or search_mode == "source" and not search_source
                            or search_mode == "progressive" and search_source):
                        raise ValueError("Invalid search mode")
                    raw_page = params.get("page", ["1"])[0]
                    raw_base = params.get("base", ["1"])[0]
                    if ("source" in params and not catalog_store.ID_RE.fullmatch(search_source)
                            or ("page" in params or "base" in params) and not search_source
                            or raw_base not in {"0", "1"}
                            or not re.fullmatch(r"[1-9][0-9]{0,4}", raw_page)
                            or int(raw_page) > library_search.MAX_PAGE):
                        raise ValueError("Invalid search page")
                    search_number = int(raw_page)
                    start_base = int(raw_base)
                sources, groups = self._public_library()
                if search_page:
                    sources = [source for source in sources if source.get("search_enabled", True)]
                eligible = {source["source_id"] for source in sources}
                for kind, gid in selected.items():
                    plural = "categories" if kind == "category" else kind + "s"
                    group = next((g for g in groups if g["kind"] == plural and g["group_id"] == gid), None)
                    eligible.intersection_update(group["source_ids"] if group else [])
                sources = [source for source in sources if source["source_id"] in eligible]
                if search_source:
                    sources = [source for source in sources if source["source_id"] == search_source]
                if search_page and query and search_mode != "progressive":
                    results = self._search_published_content(query, sources, page=search_number, start_base=start_base)
            except (ValueError, UnicodeError):
                error, code = "Invalid search or filter request.", 400
            except RuntimeError:
                error, code = "Search is busy. Try again.", 503
            if search_mode == "source":
                if error:
                    self._send_json(code, {"error": error})
                elif not results:
                    self._send_json(404, {"error": "Search source unavailable"})
                else:
                    group = results[0]
                    self._send_json(200, dict(group, results=[asdict(result) for result in group["results"]]))
                return
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            sections = []
            if not search_page:
                sections = self._library_sections(
                    sorted(sources, key=lambda source: source.get("homepage_rank", 0)),
                    lambda source: source.get("homepage_section") if not selected else None,
                    any(source.get("preserve_source_order") for source in sources),
                )
            body = environment.get_template("search.html.j2" if search_page else "library.html.j2").render(
                sources=sources, groups=results, filter_groups=groups, selected=selected,
                query=query, error=error, sections=sections, search_source=search_source,
                progressive=search_mode == "progressive").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if search_page:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif ZIM_READER_ROUTE.fullmatch(self.path):
            sid = ZIM_READER_ROUTE.fullmatch(self.path).group(1)
            try:
                records = SOURCE_CATALOG.get()["sources"]
                record = next((r for r in records if r["source_id"] == sid), None)
                state = zim_catalog.status(record, records) if record else None
                if not state or not state["published"]:
                    self._send_json(403, {"error": "ZIM collection unavailable"})
                    return
                book = zim_catalog.source_content_name(record)
                location = publication.reader_location(zim_catalog.GENERIC_READER, book) if book else None
                if location is None:
                    self._send_json(503, {"error": "ZIM reader entry unavailable"})
                    return
            except (OSError, ValueError, catalog_store.CatalogError):
                self._send_json(503, {"error": "ZIM publication state unavailable"})
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/internal/publication/zim":
            uri = self.headers.get("X-Axiom-Original-URI", "")
            try:
                records = SOURCE_CATALOG.get()["sources"]
                allowed = zim_catalog.authorize_reader_uri(records, uri)
            except (OSError, ValueError, catalog_store.CatalogError):
                allowed = False
            self.send_response(204 if allowed else 403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/api/publication":
            self._send_json(200, {item["source_id"]: state for item, state in self._pdf_publications()}
                            | {item["source_id"]: state for item, state in self._zim_publications()})
        elif self.path.rstrip("/") == "/health":
            self._send_json(200, health_response())
        elif self.path.rstrip("/") in {"/admin", "/admin/sources", "/admin/settings", "/admin/groupings"}:
            page = {"/admin/settings": "settings", "/admin/groupings": "groupings"}.get(self.path.rstrip("/"), "sources")
            self._send_admin_page(page)
        elif self.path.rstrip("/") == "/api/acquisitions":
            self._send_json(200, self._snapshot())
        elif self.path.rstrip("/") == "/api/storage-status":
            try:
                self._send_json(200, STORAGE_PROBE.status(storage_settings.load_settings()))
            except ValueError:
                self._send_json(503, {"ready": False, "error": "stored settings are invalid"})
        elif self.path == "/api/library-overview":
            try:
                sources = self._source_profiles(SOURCE_CATALOG.get(initialize=False))["sources"]
                enabled = publication.load()
                installed = [source for source in sources if source["installed_verified"]]
                published = sum(bool(enabled.get(source["source_id"])) for source in sources)
                archives = {source["artifact"]["filename"]: source["artifact"]["bytes"]
                            for source in installed if source.get("artifact")}
                self._send_json(200, {
                    "total_sources": len(sources), "installed_sources": len(installed),
                    "enabled_sources": published, "disabled_sources": len(sources) - published,
                    "installed_bytes": sum(archives.values()), "revision": RELEASE_REVISION,
                })
            except (OSError, ValueError, KeyError, TypeError):
                self._send_json(503, {"error": "Library overview unavailable"})
        elif self.path == "/api/update-sweep":
            self._send_json(200, kiwix_update_scheduler.manual_status())
        elif self.path == "/api/automatic-update-pause":
            self._send_json(200, {"paused": update_schedule.automatic_paused()})
        elif self.path == "/api/update-schedule":
            try:
                self._send_json(200, update_schedule.status())
            except (ValueError, KeyError, TypeError, OSError):
                self._send_json(503, {"error": "Update schedule unavailable"})
        elif self.path.rstrip("/") == "/api/storage-settings":
            try:
                self._send_json(200, storage_settings.load_settings())
            except ValueError:
                self._send_json(500, {"error": "stored settings are invalid"})
        elif self.path.rstrip("/") == "/api/sources":
            try:
                payload = self._source_profiles(SOURCE_CATALOG.get())
            except (catalog_store.CatalogError, OSError):
                status = 503
                payload = {"error": "source catalog unavailable; stored data was not replaced"}
            else:
                status = 200
            try:
                self._send_json(status, payload)
            except (BrokenPipeError, ConnectionResetError):
                # The client disconnected before receiving the status response.
                return
        elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", self.path.rstrip("/")):
            job_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            response = job_status_response(job_id)
            if response is None:
                self._send_json(404, {"error": "job not found"})
            else:
                self._send_json(200, response)
        else:
            self._send_json(404, {"error": "not found"})

    @staticmethod
    def _pdf_record(record):
        return record["configuration"].get("format") == "pdf"

    @staticmethod
    def _epub_record(record):
        return record["configuration"].get("format") == "epub"

    @staticmethod
    def _zim_record(record):
        return record["configuration"].get("format") == "zim"

    @staticmethod
    def _archive_record(record):
        return record["configuration"].get("format") in {"zim", "pmtiles"}

    def _manage_acquisition_batch(self, data):
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        try:
            if data.keys() == {"cancel"} and isinstance(data["cancel"], str) and re.fullmatch("[a-f0-9]{32}", data["cancel"]):
                acquisition_queue.cancel(data["cancel"])
                self._send_json(202, {"accepted": True})
                return
            ids = data.get("source_ids")
            if (data.keys() != {"revision", "source_ids", "confirm"} or data["confirm"] is not True
                    or not self._valid_revision(data["revision"])
                    or not isinstance(ids, list) or not 1 <= len(ids) <= catalog_store.MAX_ENTRIES
                    or any(not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid) for sid in ids)
                    or len(set(ids)) != len(ids)):
                self._send_json(400, {"error": "invalid confirmed batch selection"})
                return
            with publication.LOCK:
                catalog = SOURCE_CATALOG.get()
                if catalog["revision"] != data["revision"]:
                    raise ValueError("Catalog changed; preview the selection again")
                known = {item["source_id"]: item for item in catalog["sources"]}
                if any(sid not in known for sid in ids):
                    raise ValueError("Unknown selected source")
                published = publication.load()
                jobs = []
                for sid in ids:
                    try:
                        jobs.append(_batch_job(known[sid], published))
                    except ValueError as exc:
                        raise ValueError(f"{known[sid]['profile']['title']}: {exc}") from exc
                settings = storage_settings.load_settings()
                available = _batch_available(settings, {job["configuration"]["format"] for job in jobs})
                accepted = acquisition_queue.enqueue(jobs, available, settings)
        except (OSError, sqlite3.Error, catalog_store.CatalogError):
            self._send_json(503, {"error": "Batch queue, catalog or storage unavailable"})
            return
        except ValueError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(202, {"job_ids": accepted})

    def _queue_update_batch(self, data):
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        operation, selection = data.get("operation"), data.get("selection")
        if (data.keys() != {"operation", "selection", "revision"}
                or operation not in ("download-update", "publish-update")
                or not self._valid_revision(data["revision"])
                or not isinstance(selection, dict) or not 1 <= len(selection) <= catalog_store.MAX_ENTRIES
                or any(not catalog_store.ID_RE.fullmatch(sid) or not isinstance(digest, str)
                       or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{40}|[a-f0-9]{64}", digest) for sid, digest in selection.items())):
            self._send_json(400, {"error": "Invalid update selection"})
            return
        rejected = {}
        try:
            with publication.LOCK:
                catalog = SOURCE_CATALOG.get()
                if catalog["revision"] != data["revision"]:
                    raise ValueError("Catalog changed; review the updates again")
                records = {row["source_id"]: row for row in catalog["sources"]}
                jobs = []
                pending = {job["source_id"] for job in acquisition_queue.status()
                           if job["state"] in acquisition_queue.PENDING}
                for sid, digest in selection.items():
                    try:
                        if sid not in records or sid in pending:
                            raise ValueError("Source is unavailable or already queued")
                        jobs.append(_publisher_update_job(records[sid], digest, operation))
                    except (ValueError, OSError) as exc:
                        rejected[sid] = str(exc)
                accepted = []
                if jobs:
                    settings = storage_settings.load_settings()
                    available = _batch_available(settings, {records[job["source_id"]]["configuration"]["format"] for job in jobs}) if operation == "download-update" else None
                    accepted = acquisition_queue.enqueue(jobs, available, settings)
        except (OSError, sqlite3.Error, catalog_store.CatalogError):
            self._send_json(503, {"error": "Update queue, catalog or storage unavailable"})
            return
        except ValueError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(202, {"queued": dict(zip((job["source_id"] for job in jobs), accepted)),
                              "rejected": rejected})

    def _acquisition_preview(self, records, ids, *, exact_selection=False):
        rows = []
        for sid in ids:
            record = records[sid]
            configuration = record["configuration"]
            format_name = configuration["format"]
            estimate = {"state": "unsupported"}
            if self._archive_record(record):
                try:
                    estimate = zim_jobs.preview_acquisition(
                        sid, configuration, exact_selection=exact_selection)
                except (OSError, ValueError, KeyError, TypeError):
                    estimate = {"state": "unavailable"}
            elif self._pdf_record(record) or self._epub_record(record):
                limit = pdf_source.MAX_BYTES if format_name == "pdf" else epub_source.MAX_BYTES
                estimate = _preview_document_acquisition(configuration, byte_limit=limit)
            rows.append(dict(estimate, title=record["profile"]["title"], source_id=sid, format=format_name))
        for format_name in ("pdf", "epub"):
            documents = [row for row in rows if row["format"] == format_name
                         and row["state"] == "estimated"]
            if not documents:
                continue
            sizes = pdf_source.preview_installed_sizes(
                [row["source_id"] for row in documents], format_name)
            for row in documents:
                installed = sizes.get(row["source_id"])
                staging = row["staging_bytes"]
                row["installed_bytes"] = installed
                if installed is not None and staging is not None:
                    row["peak_bytes"] = installed + staging
        return {"sources": rows, "capacity": _preview_capacity(rows)}

    def _zim_publications(self, catalog=None):
        catalog = catalog or SOURCE_CATALOG.get()
        zim_status = zim_catalog.status_for_catalog(catalog["sources"])
        for record in catalog["sources"]:
            if self._zim_record(record):
                yield record, zim_status(record)

    def _search_published_content(self, query, sources, *, page=1, start_base=1):
        sources = [source for source in sources if source.get("search_enabled", True)]
        names = {source["source_id"]: source["name"] for source in sources
                 if source["category"] not in {"PDF", "EPUB"}}

        def acknowledged():
            try:
                return zim_catalog.acknowledged_reader_books(SOURCE_CATALOG.get()["sources"])
            except (OSError, ValueError):
                return {}

        targets = {
            sid: (dict(name=names[sid], path=f"/zim/content/{book}/",
                       book_id=book, port=zim_catalog.GENERIC_READER["port"]), library_search.KIWIX_ADAPTER)
            for book, sid in acknowledged().items() if sid in names
        }
        def current_documents():
            try:
                with publication.LOCK:
                    enabled = publication.load()
                    return {
                        record["source_id"]: record["configuration"]
                        for record in SOURCE_CATALOG.get()["sources"]
                        if enabled.get(record["source_id"])
                        and (self._pdf_record(record) or self._epub_record(record))
                    }
            except (OSError, ValueError):
                return {}

        configs = current_documents()
        for source in sources:
            sid = source["source_id"]
            config = configs.get(sid)
            if not config:
                continue
            kind = config["format"]
            adapter = pdf_source if kind == "pdf" else epub_source
            try:
                artifact = adapter.pin(sid, config)
            except ValueError:
                continue
            targets[sid] = (dict(name=source["name"], kind=kind, artifact=artifact,
                                 path=source["entry_path"]), library_search.DOCUMENT_ADAPTER)

        def allowed(sid, route):
            if "artifact" in route:
                config = current_documents().get(sid)
                adapter = pdf_source if route["kind"] == "pdf" else epub_source
                try:
                    return (config is not None and adapter.pin(sid, config) == route["artifact"]
                            and adapter.ready(route["artifact"], verify=False))
                except ValueError:
                    return False
            return acknowledged().get(route["book_id"]) == sid

        results = library_search.search(query, self._publication_snapshot, targets=targets, allowed=allowed,
                                        page=page, start_base=start_base)
        return results

    @staticmethod
    def _library_sections(items, section_for, preserve_order):
        if preserve_order:
            return [(section, list(members)) for section, members in groupby(items, section_for)]
        sections = {}
        for item in items:
            sections.setdefault(section_for(item), []).append(item)
        if len(sections) > 1 and None in sections:
            sections[None] = sections.pop(None)
        return list(sections.items())

    @staticmethod
    def _library_memberships(catalog):
        kind = catalog.get("library_layout", {}).get("group_by")
        memberships = {}
        for group in sorted(catalog.get("groups", []),
                            key=lambda item: (item["name"].casefold(), item["group_id"])):
            if group["kind"] == kind:
                for sid in group["source_ids"]:
                    memberships.setdefault(sid, (group["group_id"], group["name"]))
        return memberships

    def _library_order(self, catalog, published_ids=None):
        layout = catalog.get("library_layout", {})
        order = list(dict.fromkeys([
            *layout.get("source_order", []),
            *(record["source_id"] for record in catalog["sources"]
              if self._pdf_record(record) or self._epub_record(record)),
            *(record["source_id"] for record in catalog["sources"] if self._archive_record(record)),
        ]))
        if published_ids is not None and not layout.get("preserve_source_order"):
            memberships = self._library_memberships(catalog)
            if memberships:
                sections = self._library_sections(
                    (sid for sid in order if sid in published_ids), memberships.get, False)
                return [sid for _, members in sections for sid in members] + [
                    sid for sid in order if sid not in published_ids]
        return order

    def _public_library(self):
        """Return only published sources and groups containing those sources."""
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                return [], []
            sources = []
            for record, state in self._pdf_publications():
                if state["published"]:
                    profile = record["profile"]
                    sources.append(dict(source_id=record["source_id"], entry_path=state["entry_path"],
                                        symbol="E" if self._epub_record(record) else "P", category="EPUB" if self._epub_record(record) else "PDF", name=profile["title"],
                                        description=profile.get("description", ""),
                                        icon_png=profile.get("icon_png", ""),
                                        language=profile.get("language") or "Unspecified language"))
            for record, state in self._zim_publications(catalog):
                if state["published"]:
                    profile = record["profile"]
                    sources.append(dict(source_id=record["source_id"],
                                        entry_path=state["entry_path"], symbol="Z",
                                        category="Kiwix", name=profile["title"],
                                        description=profile.get("description", ""),
                                        icon_png=profile.get("icon_png", ""),
                                        language=profile.get("language") or "Unspecified language"))
            for record in catalog["sources"]:
                if record["configuration"].get("format") == "pmtiles":
                    state = pmtiles_source.status(record)
                    if state["published"]:
                        profile = record["profile"]
                        sources.append(dict(source_id=record["source_id"], entry_path=state["entry_path"],
                                            symbol="M", category="Map", name=profile["title"],
                                            description=profile.get("description", ""), icon_png=profile.get("icon_png", ""),
                                            language=profile.get("language") or "Unspecified language", search_enabled=False))
            search_disabled = {record["source_id"] for record in catalog["sources"]
                               if record["configuration"].get("search_enabled") == "no"}
            for source in sources:
                if source["source_id"] in search_disabled:
                    source["search_enabled"] = False
            ids = {source["source_id"] for source in sources}
            groups = [dict(group, source_ids=[sid for sid in group["source_ids"] if sid in ids])
                      for group in catalog.get("groups", [])]
            groups = sorted((group for group in groups if group["source_ids"]),
                            key=lambda group: (group["name"].casefold(), group["group_id"]))
            layout = catalog.get("library_layout", {})
            order = {sid: position for position, sid in enumerate(self._library_order(catalog))}
            sections = self._library_memberships(catalog)
            for source in sources:
                sid = source["source_id"]
                source["memberships"] = [group["name"] for group in groups if sid in group["source_ids"]]
                source["homepage_rank"] = order.get(sid, len(order))
                source["homepage_section"] = sections.get(sid)
                source["preserve_source_order"] = layout.get("preserve_source_order", False)
            return sources, groups

    def _pdf_publications(self):
        try:
            catalog = SOURCE_CATALOG.get()
        except (catalog_store.CatalogError, OSError):
            return
        for record in catalog["sources"]:
            if self._pdf_record(record):
                artifact = select_lifecycle(record["source_id"], record["configuration"])["artifact"]
                yield record, pdf_source.status(record, artifact)
            elif self._epub_record(record):
                artifact = select_lifecycle(record["source_id"], record["configuration"])["artifact"]
                yield record, epub_source.status(record, artifact)

    def _serve_map_asset(self):
        path = _map_asset_path(self.path)
        try:
            body = (ADMIN_TEMPLATE_DIR / "map-assets" / path.rsplit("/", 1)[-1]).read_bytes()
        except OSError:
            self._send_json(404, {"error": "map asset unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", MAP_ASSET_PATHS[path])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pmtiles(self, sid, archive):
        # PMTiles only needs bounded random-access reads; refuse whole-archive GETs.
        try:
            with publication.LOCK, ExitStack() as files:
                record = next((item for item in SOURCE_CATALOG.get()["sources"]
                               if item["source_id"] == sid and item["configuration"]["format"] == "pmtiles"), None)
                if not record or not pmtiles_source.status(record)["published"]:
                    raise ValueError("Map not published")
                job = zim_jobs.status(sid)
                artifact = zim_jobs.installed(job, job["configuration"])
                if not artifact:
                    raise ValueError("Map unavailable")
                if not archive:
                    body = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True).get_template(
                        "map-reader.html.j2").render(record=record, path=self.path + "/map.pmtiles").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                total = artifact["bytes"]
                etag = '"' + artifact["sha256"] + '"'
                match = re.fullmatch(r"bytes=([0-9]{1,13})-([0-9]{1,13})", self.headers.get("Range", ""))
                start, end = map(int, match.groups()) if match else (-1, -1)
                end = min(end, total - 1)
                if not 0 <= start <= end < total or end - start + 1 > 16 * 1024 * 1024:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("If-Match") not in (None, etag, "*"):
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                parent, mount, _ = files.enter_context(pdf_source._directory("library", format_name="pmtiles"))
                stream = files.enter_context(zim_jobs.open_installed_archive(parent, mount, job, artifact))
                stream.seek(start)
                body = stream.read(end - start + 1)
                if len(body) != end - start + 1:
                    raise ValueError("Incomplete map range")
        except (OSError, ValueError, KeyError, TypeError):
            self._send_json(403, {"error": "map unavailable"})
            return
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Panning and zooming routinely cancel outstanding tile requests.
            pass

    def _serve_pdfjs_asset(self):
        """Serve only trusted installed renderer assets, never arbitrary host files."""
        relative = self.path[len(PDFJS_PREFIX):]
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            self._send_json(404, {"error": "asset not found"})
            return
        types = {".mjs": "text/javascript", ".js": "text/javascript",
                 ".css": "text/css", ".wasm": "application/wasm",
                 ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
                 ".ttf": "font/ttf", ".otf": "font/otf", ".icc": "application/octet-stream",
                 ".json": "application/json", ".ftl": "text/plain",
                 ".svg": "image/svg+xml", ".png": "image/png"}
        try:
            root = PDFJS_ROOT.resolve(strict=True)
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or path.suffix not in types or not path.is_file():
                raise ValueError("not an asset")
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("asset too large")
            body = path.read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"error": "PDF.js asset unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", types[path.suffix])
        self.send_header("Content-Length", str(len(body)))
        # A dedicated worker inherits its own response CSP. Permit only
        # local renderer imports and font/CMap requests, never remote fetches.
        policy = "default-src 'none'"
        if relative == "build/pdf.worker.mjs":
            policy += "; script-src 'self'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def _serve_epub(self, sid, resource, size):
        try:
            with publication.LOCK:
                if not publication.load().get(sid):
                    raise ValueError("unpublished")
                record = next((item for item in SOURCE_CATALOG.get()["sources"]
                               if item["source_id"] == sid and self._epub_record(item)), None)
                if record is None:
                    raise ValueError("unknown EPUB")
                artifact = epub_source.pin(sid, record["configuration"])
                with epub_source.installed(artifact, verify=False) as stream:
                    data = stream.read(artifact["bytes"] + 1)
                # Validate the exact immutable byte snapshot that will be served.
                epub_source._verify(BytesIO(data), artifact)
            if resource == "document.epub":
                body = data
            else:
                index = int(resource.split("/")[1]) if resource else 0
                content, labels = epub_source.chapter_content(data, index)
                environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
                body = environment.get_template("epub-reader.html.j2").render(
                    record=record, base=epub_source.ROUTE_PREFIX + sid, chapter=index,
                    labels=labels, content=content, size=size or "medium").encode("utf-8")
        except (OSError, ValueError):
            self._send_json(403, {"error": "document unavailable"})
            return
        self.send_response(200)
        if resource == "document.epub":
            self.send_header("Content-Type", "application/epub+zip")
            self.send_header("Content-Disposition", f'attachment; filename="{sid}.epub"')
        else:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; sandbox allow-downloads")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pdf(self, sid, document):
        with ExitStack() as files:
            try:
                with publication.LOCK:
                    if not publication.load().get(sid):
                        raise ValueError
                    record = next((item for item in SOURCE_CATALOG.get()["sources"]
                                   if item["source_id"] == sid and self._pdf_record(item)), None)
                    if record is None:
                        raise ValueError
                    artifact = pdf_source.pin(sid, record["configuration"])
                    stream = files.enter_context(pdf_source.installed(artifact, verify=True))
            except (OSError, ValueError):
                self._send_json(403, {"error": "document unavailable"})
                return
            if not document:
                environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
                body = environment.get_template("pdf-reader.html.j2").render(
                    record=record, path=self.path + "/document.pdf").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{sid}.pdf"')
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.send_header("Content-Length", str(artifact["bytes"]))
            self.end_headers()
            remaining = artifact["bytes"]
            while remaining and (block := stream.read(min(remaining, pdf_source.CHUNK))):
                self.wfile.write(block)
                remaining -= len(block)

    def _snapshot(self):
        monitor = getattr(self.server, "acquisition_monitor", None)
        try:
            if monitor:
                snap = monitor.snapshot()
                jobs = snap["acquisitions"]
                if (type(snap["stale"]) is bool and isinstance(jobs, list)
                        and all(isinstance(job, dict) and isinstance(job.get("source_id"), str)
                                for job in jobs)):
                    return snap
            elif getattr(self.server, "managed_acquisition_only", False):
                return {"acquisitions": [], "stale": False}
        except Exception:
            pass
        return {"acquisitions": [], "stale": True}

    @staticmethod
    def _matched_snapshot(snapshot, artifacts):
        jobs = []
        for job in snapshot["acquisitions"]:
            artifact = artifacts.get(job["source_id"])
            if artifact and all(job.get(field) == artifact[key] for field, key in (
                    ("filename", "filename"), ("expected_bytes", "bytes"), ("sha256", "sha256"))):
                jobs.append(job)
        return dict(snapshot, acquisitions=jobs)

    @staticmethod
    def _lifecycles(catalog):
        return {record["source_id"]: select_lifecycle(record["source_id"], record["configuration"])
                for record in catalog["sources"]}

    def _publication_snapshot(self):
        try:
            catalog = SOURCE_CATALOG.get()
            lifecycles = self._lifecycles(catalog)
            snapshot = self._matched_snapshot(self._snapshot(), {sid: life["artifact"] for sid, life in lifecycles.items()})
            return dict(snapshot, sources=catalog["sources"])
        except (catalog_store.CatalogError, OSError):
            return {"acquisitions": [], "stale": True}

    def _source_profiles(self, catalog):
        snapshot = self._snapshot()
        lifecycles = self._lifecycles(catalog)
        artifacts = {sid: life["artifact"] for sid, life in lifecycles.items()}
        matched = self._matched_snapshot(snapshot, artifacts)
        jobs = {job["source_id"]: job for job in matched["acquisitions"]}
        zim_status = zim_catalog.status_for_catalog(catalog["sources"])
        paused = update_schedule.automatic_paused()
        local_now = datetime.now(update_schedule.timezone())
        attempts = {}
        if any(row["configuration"].get("update_policy") in update_discovery_config.UNATTENDED
               for row in catalog["sources"]):
            try:
                for attempt in acquisition_queue.automatic_attempts():
                    attempts.setdefault(attempt["source_id"], []).append(attempt)
            except (OSError, sqlite3.Error):
                pass  # Source profiles remain available when queue status is unknown.
        sources = []
        for record in catalog["sources"]:
            sid = record["source_id"]
            busy = source_busy(snapshot, sid)
            update_check = None
            discovery = update_discovery_config.settings(record["configuration"])
            state = None
            if self._pdf_record(record):
                busy = False
                pdf_state = pdf_source.status(record, artifacts[sid])
                state = pdf_state
            elif self._epub_record(record):
                busy = False
                state = epub_source.status(record, artifacts[sid])
            verified = (bool(state and state["verified"]) if self._pdf_record(record) or self._epub_record(record)
                        else False)
            if (verified and (self._pdf_record(record) or self._epub_record(record))
                    and isinstance(artifacts.get(sid), dict)
                    and discovery["method"] == "head"):
                update_check = update_check_store.load(sid, update_discovery_config.cache_identity(
                    record["configuration"], artifacts[sid]["sha256"]))
            job = jobs.get(sid)
            installed_edition = None
            replacement_available = False
            if lifecycles[sid].get("zim_download"):
                try:
                    local_job = zim_jobs.status(sid)
                    matches_profile = zim_jobs.matches(local_job, record["configuration"])
                    if local_job:
                        busy = bool(local_job.get("active")) or (matches_profile
                            and local_job["state"] in {"paused", "failed", "interrupted"}
                            and zim_jobs.staged_size(sid) > 0)
                        if matches_profile:
                            job = local_job
                        # The profile can describe a staged update. The serving
                        # ledger still owns the installed archive and its size.
                        artifact = (zim_jobs.installed(local_job, local_job["configuration"])
                                    if local_job.get("verified") is True
                                    and local_job.get("active") is False else None)
                        if artifact:
                            job = local_job
                            lifecycles[sid]["artifact"] = artifact
                            verified = True
                            replacement_available = not matches_profile
                            identity = kiwix_update_discovery.installed_identity_from_job(local_job, artifact)
                            if identity:
                                installed_edition = identity["edition"]
                            update_check = update_check_store.load(sid, artifact["sha256"])
                        elif matches_profile and local_job["state"] == "completed":
                            job = dict(local_job, state="unavailable", verified=False, detail="Installed archive unavailable")
                    else:
                        busy = False
                except (ValueError, OSError, KeyError, TypeError):
                    busy = True
            if self._archive_record(record):
                state = zim_status(record) if self._zim_record(record) else pmtiles_source.status(record)
                verified = bool(state["verified"])
            publication_filter = ("unknown" if state and state.get("error") else
                                  "published" if state and state["published"] else "unpublished")
            download_filter = "unknown"
            if verified:
                download_filter = "downloaded"
            elif not (state and state.get("error")):
                if self._pdf_record(record) or self._epub_record(record):
                    if not artifacts[sid]:
                        download_filter = "not-downloaded"
                    else:
                        # A missing file, corrupt file or unavailable mount cannot all
                        # be distinguished by the existing verification result.
                        download_filter = "unknown"
                elif lifecycles[sid].get("zim_download") and not busy:
                    download_filter = "not-downloaded"
                elif not snapshot["stale"] and not busy and (not artifacts[sid]
                        or (job and job.get("state") == "not_started")):
                    download_filter = "not-downloaded"
            replacement = candidate = None
            if lifecycles[sid].get("zim_download"):
                try:
                    candidate = zim_jobs.candidate_status(sid)
                    replacement = _public_zim_replacement(candidate)
                    if replacement and replacement["state"] in {"failed", "cancelled", "interrupted"}:
                        # Never offer discard while partial or promoted bytes
                        # remain. Use the same candidate snapshot as the UI.
                        replacement["can_discard"] = (
                            not zim_jobs.staged_present(sid + "-candidate")
                            and not candidate.get("artifact")
                        )
                except (ValueError, OSError):
                    replacement = {"state": "unavailable", "error": "Replacement status unavailable"}
            source_attempts = attempts.get(sid, [])
            automatic = _automatic_status(record["configuration"], paused, local_now,
                                          source_attempts[0]["state"] if source_attempts else "")

            sources.append(dict(record, **lifecycles[sid], acquisition=_public_acquisition(job), replacement=replacement, replacement_available=replacement_available,
                                update_check=update_check if discovery["method"] != "manual" else None,
                                discovery=discovery, automatic_update=automatic,
                                installed_verified=verified, installed_edition=installed_edition,
                                publication=state, mutation_blocked=busy,
                                publication_filter=publication_filter, download_filter=download_filter))
        published_ids = {source["source_id"] for source in sources if source["publication_filter"] == "published"}
        stages = [stage for stage in map(_update_stage, sources) if stage]
        return dict(catalog, sources=sources, library_order=self._library_order(catalog, published_ids),
                    acquisition_stale=snapshot["stale"],
                    update_summary=_source_update_summary(sources),
                    available_updates=len(stages),
                    update_stages={stage: stages.count(stage) for stage in ("download", "publish")})

    def _send_admin_page(self, page: str = "sources") -> None:
        """Render exactly the persisted source profiles."""
        environment = Environment(
            loader=FileSystemLoader(ADMIN_TEMPLATE_DIR),
            autoescape=True,
        )
        template = environment.get_template("admin-page.html.j2")
        try:
            catalog_snapshot = SOURCE_CATALOG.get() if page in {"sources", "groupings"} else {"revision": None, "sources": []}
        except (catalog_store.CatalogError, OSError):
            self._send_json(503, {"error": "source catalog unavailable; stored data was not replaced"})
            return
        body = template.render(
            admin_page=page,
            catalog=self._source_profiles(catalog_snapshot) if page == "sources" else catalog_snapshot if page == "groupings" else None,
            selection_presets={key: value[0] for key, value in library_selection.PRESETS.items()},
            editable_fields=sorted(catalog_store.EDITABLE),
            action_labels=ACTION_LABELS,
            password_change_enabled=bool(getattr(getattr(self.server, "axiom_config", None), "store", None)),
            session_csrf_token=getattr(self, "_session_csrf", None),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_admin_login(self, status=200, error=""):
        environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
        body = environment.get_template("admin-login.html.j2").render(error=error).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_login_error(self, form_login, status, error):
        if form_login:
            self._send_admin_login(status, error)
        else:
            self._send_json(status, {"error": error.rstrip(".").lower()})

    def _sign_in_admin(self, *, form_login):
        import hmac
        config = getattr(self.server, "axiom_config", None)
        if config is None or config.store is None:
            self._send_login_error(form_login, 503, "Administrator authentication unavailable.")
            return
        if form_login:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                fields = parse_qs(raw.decode("ascii"), keep_blank_values=True,
                                  strict_parsing=True, errors="strict", max_num_fields=2)
            except ValueError:
                fields = {}
            data = {key: values[0] for key, values in fields.items() if len(values) == 1}
        else:
            data = self._read_body()
        if (set(data) != {"username", "password"} or
                not all(isinstance(data[k], str) for k in data) or
                len(data["username"]) > 64 or len(data["password"]) > 256):
            self._send_login_error(form_login, 400, "Invalid sign-in request.")
            return
        if not (data["username"].isascii() and hmac.compare_digest(data["username"], config.username) and
                config.store.verify(data["password"])):
            self._send_login_error(form_login, 403, "Invalid username or password.")
            return
        try:
            token, _ = ADMIN_SESSIONS.issue(config.store)
        except (OSError, ValueError):
            self._send_login_error(form_login, 503, "Authentication state unavailable.")
            return
        secure = config.proxied and self.headers.get("X-Forwarded-Proto") == "https"
        self.send_response(303 if form_login else 204)
        if form_login:
            self.send_header("Location", "/admin")
        self.send_header("Set-Cookie", admin_sessions.cookie_header(token, secure=secure))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - http.server convention
        global ENDPOINT_TEST_NEXT
        if not self._guard(post=True):
            return
        if self.path in {"/admin/login", "/api/admin/login"}:
            self._sign_in_admin(form_login=self.path == "/admin/login")
            return
        if self.path == "/api/admin/logout":
            ADMIN_SESSIONS.revoke(getattr(self, "_session_token", None))
            config = getattr(self.server, "axiom_config", None)
            secure = bool(config and config.proxied and self.headers.get("X-Forwarded-Proto") == "https")
            self.send_response(204)
            self.send_header("Set-Cookie", admin_sessions.clear_cookie_header(secure=secure))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/pdf-upload":
            self._upload_pdf()
            return
        if self.path.rstrip("/") == "/api/admin/password":
            config = getattr(self.server, "axiom_config", None)
            if config is None or config.store is None:
                self._send_json(503, {"error": "admin authentication is not configured"})
                return
            # A browser password change must explicitly originate from this page.
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if (set(data) != {"current_password", "new_password", "confirm_password"}
                    or any(not isinstance(value, str) for value in data.values())
                    or data["new_password"] != data["confirm_password"]):
                self._send_json(400, {"error": "password confirmation does not match"})
                return
            try:
                changed = config.store.change(data["current_password"], data["new_password"])
            except ValueError:
                self._send_json(400, {"error": "choose a different password of 8–256 printable non-space ASCII characters"})
                return
            except OSError:
                self._send_json(503, {"error": "credential update failed"})
                return
            if not changed:
                self._send_json(403, {"error": "current password is incorrect"})
                return
            ADMIN_SESSIONS.clear()  # revoke browser sessions after credential rotation
            self._send_json(200, {"changed": True})
        elif self.path == "/api/update-sweep":
            if not self.headers.get("Origin") or self._read_body() != {"confirm": True}:
                self._send_json(400, {"error": "Confirm the metadata check from Admin"})
                return
            if not kiwix_update_scheduler.start_manual():
                self._send_json(409, {"error": "An update check is already running"})
                return
            self._send_json(202, {"state": "running"})
        elif self.path == "/api/automatic-update-pause":
            data = self._read_body()
            if not isinstance(data, dict) or data.keys() != {"paused"} or type(data["paused"]) is not bool:
                self._send_json(400, {"error": "Specify whether automatic updates are paused"})
                return
            try:
                with publication.LOCK:
                    update_schedule.set_automatic_pause(data["paused"])
                self._send_json(200, data)
            except OSError:
                self._send_json(503, {"error": "Automatic update pause could not be saved"})
        elif self.path == "/api/update-schedule":
            data = self._read_body()
            try:
                update_schedule.validate(data)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                update_schedule.save(data)
                self._send_json(200, update_schedule.status())
            except (ValueError, KeyError, TypeError, OSError):
                self._send_json(503, {"error": "Update schedule could not be saved"})
        elif self.path == "/api/update-batch":
            self._queue_update_batch(self._read_body())
        elif self.path == "/api/update-endpoint-test":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            try:
                fields = {"format", "source_url"} | update_discovery_config.FIELDS
                SOURCE_CATALOG._validate_fields(data, fields, required={"format"})
                selected = update_discovery_config.settings(data)
                if selected["method"] == "manual":
                    self._send_json(200, update_discovery_config.probe_endpoint(selected))
                    return
                if not ENDPOINT_TEST_LOCK.acquire(blocking=False):
                    self._send_json(429, {"error": "Another endpoint test is in progress"})
                    return
                try:
                    now = time.monotonic()
                    if now < ENDPOINT_TEST_NEXT:
                        self._send_json(429, {"error": "Wait a few seconds before testing another endpoint"})
                        return
                    ENDPOINT_TEST_NEXT = now + 3
                    result = update_discovery_config.probe_endpoint(selected)
                finally:
                    ENDPOINT_TEST_LOCK.release()
            except (catalog_store.CatalogError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
        elif self.path == "/api/update-check":
            # Explicit metadata-only request. No archive transfer, activation,
            # publication mutation or untrusted caller-provided URL.
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if (set(data) != {"source_id", "revision"}
                    or not isinstance(data["source_id"], str)
                    or not catalog_store.ID_RE.fullmatch(data["source_id"])
                    or not self._valid_revision(data["revision"])):
                self._send_json(400, {"error": "invalid update-check request"})
                return
            try:
                catalog = SOURCE_CATALOG.get()
            except (OSError, catalog_store.CatalogError):
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            if data["revision"] != catalog["revision"]:
                self._send_json(409, {"error": "catalog revision changed"})
                return
            sid = data["source_id"]
            record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
            discovery = update_discovery_config.settings(record["configuration"]) if record else None
            if not discovery or discovery["method"] == "manual":
                self._send_json(400, {"error": "source does not support update checks"})
                return
            with update_diagnostics.attempt(sid, discovery, trigger="manual") as diagnostic:
                if record["configuration"]["format"] in {"zim", "pmtiles"}:
                    result = kiwix_update_discovery.refresh_checked_source(sid)
                else:
                    result = document_update_discovery.check_installed_document(record)
                diagnostic.update(result)
            self._send_json(200, dict(source_id=sid, **result))
        elif self.path == "/api/upstream-kiwix-catalog":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if data.keys() not in ({"family", "language"}, {"family", "language", "edition", "variant"}):
                self._send_json(400, {"error": "provide a Kiwix family and language"})
                return
            try:
                result = (upstream_catalog.inspect_edition(**data) if "edition" in data
                          else upstream_catalog.refresh(**data))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
        elif self.path == "/api/library-selection":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if data.keys() not in ({"revision", "source_ids"}, {"revision", "source_ids", "transfer_manifest"},
                                   {"manifest"}, {"preset"}, {"transfer_preview"}) or (
                    "transfer_manifest" in data and data["transfer_manifest"] is not True) or (
                    "source_ids" in data and not self._valid_revision(data["revision"])):
                self._send_json(400, {"error": "invalid selection request"})
                return
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            if "source_ids" in data and data["revision"] != catalog["revision"]:
                self._send_json(409, {"error": "catalog revision changed"})
                return
            known = {record["source_id"]: record for record in catalog["sources"]}
            try:
                if "source_ids" in data:
                    exporter = local_transfer.export_manifest if data.get("transfer_manifest") else library_selection.export_selection
                    result = {"manifest": exporter(known, data["source_ids"])}
                elif "transfer_preview" in data:
                    result = {"transfer_artifacts": local_transfer.preview_manifest(known, data["transfer_preview"])}
                else:
                    matches = (library_selection.preview_preset(known, data["preset"]) if "preset" in data
                               else library_selection.preview_selection(known, data["manifest"]))
                    ids = [source["source_id"] for source in matches if source["state"] == "match"]
                    result = {"revision": catalog["revision"], "sources": matches,
                              "storage": self._acquisition_preview(known, ids, exact_selection=True)}
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
        elif self.path == "/api/acquisition-batch":
            self._manage_acquisition_batch(self._read_body())
        elif self.path == "/api/acquisition-preview":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            ids = data.get("source_ids")
            if (data.keys() != {"revision", "source_ids"} or not self._valid_revision(data.get("revision"))
                    or not isinstance(ids, list) or not 1 <= len(ids) <= catalog_store.MAX_ENTRIES
                    or any(not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid) for sid in ids)
                    or len(set(ids)) != len(ids)):
                self._send_json(400, {"error": "invalid acquisition selection"})
                return
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            if data["revision"] != catalog["revision"]:
                self._send_json(409, {"error": "catalog revision changed"})
                return
            records = {record["source_id"]: record for record in catalog["sources"]}
            if any(sid not in records for sid in ids):
                self._send_json(400, {"error": "unknown selected source"})
                return
            self._send_json(200, self._acquisition_preview(records, ids))
        elif self.path == "/api/library-layout":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if data.keys() != {"revision", "layout"}:
                self._send_json(400, {"error": "invalid library layout request"})
                return
            try:
                with publication.LOCK:
                    saved = SOURCE_CATALOG.set_library_layout(data["revision"], data["layout"])
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "library layout write failed"})
                return
            self._send_json(200, {"revision": saved["revision"], "layout": saved.get("library_layout")})
        elif self.path == "/api/groups":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            required = {"operation", "kind", "group_id", "revision"}
            if not required <= data.keys() or data.keys() - (required | {"name", "source_ids", "group_ids"}):
                self._send_json(400, {"error": "invalid group mutation"})
                return
            try:
                with publication.LOCK:
                    result = SOURCE_CATALOG.mutate_group(**data)
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "group catalog write failed"})
                return
            self._send_json(200, result)
        elif self.path.rstrip("/") == "/api/sources":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if set(data) != {"operation", "source_id", "fields", "revision"}:
                self._send_json(400, {"error": "invalid source mutation"})
                return
            sid = data.get("source_id")
            try:
                with publication.LOCK:
                    operation, fields, revision = data["operation"], data["fields"], data["revision"]
                    catalog = SOURCE_CATALOG.get()
                    existing = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                    is_pdf = (existing and (self._pdf_record(existing) or self._epub_record(existing))) or (
                        existing is None and isinstance(sid, str)
                        and isinstance(fields, dict) and fields.get("format") in {"pdf", "epub"})
                    busy = not is_pdf and source_busy(self._snapshot(), sid)
                    if isinstance(sid, str):
                        local_job = zim_jobs.status(sid)
                        if local_job and local_job.get("active"):
                            busy = True
                    unpublished = False
                    if operation == "remove":
                        # Validate all preconditions before changing publication.
                        SOURCE_CATALOG._validate_id(sid)
                        if not self._valid_revision(revision) or revision != catalog["revision"]:
                            raise catalog_store.RevisionConflict("catalog revision changed")
                        if existing is None or not isinstance(fields, dict) or fields:
                            raise catalog_store.CatalogError("remove requires an existing source and empty fields")
                        if busy:
                            raise catalog_store.CatalogError("source has active or unresolved acquisition")
                        try:
                            if publication.load().get(sid, False):
                                publication.save(sid, False)
                                unpublished = True
                                if self._zim_record(existing):
                                    zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])
                        except (OSError, ValueError) as exc:
                            raise catalog_store.RevisionConflict("could not unpublish source; removal refused") from exc
                    elif isinstance(sid, str) and (is_pdf or
                            existing and self._archive_record(existing) and isinstance(fields, dict)
                            and fields.get("format", existing["configuration"]["format"]) != existing["configuration"]["format"]):
                        self._guard_published_mutation(operation, sid, fields, revision)
                    try:
                        result = SOURCE_CATALOG.mutate(operation, sid, fields, revision, busy)
                    except (catalog_store.CatalogError, OSError) as exc:
                        if unpublished:
                            # Never restore public access automatically after a failed removal.
                            raise catalog_store.RevisionConflict("source unpublished but profile removal failed; reload and retry removal") from exc
                        raise
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "source catalog write failed"})
                return
            self._send_json(200, self._source_profiles(result))
        elif self.path == "/api/publication":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            source_id, enabled = data.get("source_id"), data.get("enabled")
            if (set(data) not in ({"source_id", "enabled", "revision"}, {"source_id", "enabled", "revision", "acknowledge_rights"})
                    or ("acknowledge_rights" in data and (type(data["acknowledge_rights"]) is not bool or not enabled))
                    or not isinstance(source_id, str)
                    or not catalog_store.ID_RE.fullmatch(source_id) or type(enabled) is not bool
                    or not self._valid_revision(data["revision"])):
                self._send_json(400, {"error": "invalid publication request"})
                return
            with publication.LOCK:
                try:
                    catalog = SOURCE_CATALOG.get()
                except (catalog_store.CatalogError, OSError):
                    self._send_json(503, {"error": "source catalog unavailable"})
                    return
                if data["revision"] != catalog["revision"]:
                    self._send_json(409, {"error": "catalog revision changed"})
                    return
                record = next((item for item in catalog["sources"] if item["source_id"] == source_id), None)
                is_epub = record is not None and self._epub_record(record)
                is_pdf = record is not None and self._pdf_record(record)
                is_document = is_pdf or is_epub
                is_zim = record is not None and self._archive_record(record)
                if not (is_document or is_zim):
                    self._send_json(400, {"error": "source has no publication adapter"})
                    return
                if "acknowledge_rights" in data and not (is_document or is_zim):
                    self._send_json(400, {"error": "rights acknowledgement is for documents and ZIMs only"})
                    return
                if enabled and record is not None and (is_document or is_zim):
                    if is_zim:
                        try:
                            job = zim_jobs.status(source_id)
                            artifact = (zim_jobs.installed(job, record["configuration"])
                                        if job and job.get("verified") is True and job.get("active") is False else None)
                        except (OSError, ValueError):
                            artifact = None
                        if not artifact:
                            self._send_json(409, {"error": "a completed, verified, matching ZIM archive is required"})
                            return
                    else:
                        artifact = select_lifecycle(source_id, record["configuration"])["artifact"]
                        if not (epub_source.ready(artifact) if is_epub else pdf_source.ready(artifact, verify=True)):
                            self._send_json(409, {"error": "matching verified EPUB content is required" if is_epub else "matching verified PDF content is required"})
                            return
                    if not record["configuration"].get("license", "").strip():
                        if data.get("acknowledge_rights") is not True:
                            self._send_json(409, {"error": "Confirm you have permission to make this ZIM available to library users" if is_zim else "Confirm you have permission to make this EPUB available to library users" if is_epub else "Confirm you have permission to make this PDF available to library users"})
                            return
                        try:
                            SOURCE_CATALOG.mutate("edit", source_id, {
                                "license": "Administrator confirmed permission to make this " + ("ZIM" if is_zim else "EPUB" if is_epub else "PDF") + " available in this library; no publisher license independently verified."
                            }, data["revision"])
                        except catalog_store.RevisionConflict:
                            self._send_json(409, {"error": "catalog revision changed"})
                            return
                        except (catalog_store.CatalogError, OSError):
                            self._send_json(503, {"error": "Could not record publication acknowledgement"})
                            return
                try:
                    if enabled and not publication.load().get(source_id):
                        catalog = SOURCE_CATALOG.get()
                        layout = catalog.get("library_layout", {})
                        published_ids = None
                        if layout.get("group_by", "none") != "none" and not layout.get("preserve_source_order"):
                            sources, _ = self._public_library()
                            published_ids = {source["source_id"] for source in sources}
                        order = [sid for sid in self._library_order(catalog, published_ids) if sid != source_id]
                        order.append(source_id)
                        catalog = SOURCE_CATALOG.set_library_layout(catalog["revision"], {
                            "source_order": order,
                            "group_by": layout.get("group_by", "none"),
                            "preserve_source_order": True,
                        })
                    publication.save(source_id, enabled)
                except (OSError, ValueError):
                    self._send_json(503, {"error": "could not save publication state"})
                    return
            if is_zim and self._zim_record(record):
                # Never restart a reader from HTTP. Generate the new private
                # manifest and wait for independently monitored Kiwix OPDS.
                zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])
            if is_document and enabled:
                library_search.document_search.schedule(artifact, "epub" if is_epub else "pdf")
            self._send_json(200, {"enabled": enabled, "revision": catalog["revision"]})
        elif self.path.rstrip("/") == "/api/storage-settings":
            data = self._read_body()
            try:
                settings = storage_settings.validate_settings(data)
            except ValueError:
                self._send_json(400, {"error": "invalid storage settings"})
                return
            try:
                with publication.LOCK:
                    try:
                        current = storage_settings.load_settings()
                    except ValueError:
                        current = None  # Explicit save can replace invalid preferences.
                    if current != settings:
                        with zim_jobs.worker_lock():
                            self._check_storage_change()
                            settings = storage_settings.save_settings(settings)
            except BlockingIOError:
                self._send_json(409, {"error": "Finish or resolve the acquisition before changing storage settings"})
                return
            except (ValueError, OSError):
                self._send_json(503, {"error": "Storage settings could not be saved safely"})
                return
            self._send_json(200, settings)
        elif self.path.rstrip("/") == "/api/action":
            data = self._read_body()
            if data.get("action") == "zim-replacement-update":
                self._download_zim_update(data)

            elif data.get("action") == "zim-replacement-delete-verified":
                self._delete_staged_update(data)
            elif data.get("action") in {"zim-download", "zim-resume", "zim-pause", "zim-cancel", "zim-cancel-keep", "zim-restart", "zim-retry", "zim-revalidate", "zim-replacement-prepare", "zim-replacement-discard", "zim-replacement-activate", "zim-replacement-rollback", "zim-replacement-download", "zim-replacement-cancel", "zim-delete-installed"}:
                self._start_zim(data)
            elif data.get("action") == "delete-downloaded-files":
                self._delete_downloaded_files(data)
            elif data.get("action") == "import":
                self._import_pdf(data)
            elif data.get("action") in {"pdf-download", "epub-download", "epub-add-download"}:
                self._download_pdf(data)
            else:
                self._refuse_acquisition(data)
        else:
            self._send_json(404, {"error": "not found"})


    def _check_storage_change(self):
        """Check local acquisition records without probing the managed mount."""
        monitor = getattr(self.server, "acquisition_monitor", None)
        if monitor is not None:
            snapshot = self._snapshot()
            if snapshot["stale"] or any(
                    job.get("active") is not False or
                    job.get("state") not in {"completed", "not_started", "unavailable"}
                    for job in snapshot["acquisitions"]):
                raise BlockingIOError("External acquisition unresolved")
        with _active_downloads_lock:
            if any(job["process"].poll() is None for job in _jobs.values()):
                raise BlockingIOError("External acquisition active")
        for record in SOURCE_CATALOG.get()["sources"]:
            sid = record["source_id"]
            job = zim_jobs.read(sid)
            if job and (job["state"] not in {"completed", "cancelled"}
                        or job["state"] == "cancelled" and job.get("downloaded_bytes") not in (0, None)):
                raise BlockingIOError("ZIM acquisition unresolved")
            replacement = zim_jobs.read_candidate(sid)
            if not replacement:
                continue
            if replacement["state"] == "activated":
                expected = dict(source_id=sid, state="completed", active=False, verified=True,
                                configuration=replacement["configuration"], artifact=replacement["artifact"])
            elif replacement["state"] == "rolled_back":
                expected = replacement["previous_job"]
            else:
                raise BlockingIOError("ZIM replacement unresolved")
            if job != expected:
                raise BlockingIOError("ZIM replacement transition unresolved")

    @staticmethod
    def _valid_revision(revision):
        return type(revision) is int and revision >= 0

    def _import_pdf(self, data):
        sid = data.get("source_id")
        if (set(data) != ({"source_id", "action", "revision", "confirm_restart"} if data.get("action") == "zim-restart" else {"source_id", "action", "revision"})
                or (data.get("action") == "zim-restart" and data.get("confirm_restart") is not True)
                or not isinstance(sid, str)
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "import requires a source ID and catalog revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if record is None or not self._pdf_record(record):
                    raise ValueError("source has no PDF import adapter")
                artifact = pdf_source.pin(sid, record["configuration"])
                pdf_source.import_local(sid, artifact)
                if not record["profile"].get("language"):
                    language = pdf_language.installed_language(artifact)
                    if language:
                        SOURCE_CATALOG.mutate("edit", sid, {"language": language}, data["revision"])
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "PDF inbox or local storage unavailable"})
                return
        self._send_json(200, {"accepted": True, "installed_verified": True})

    def _upload_pdf(self):
        # Raw bytes; never accept a client filename or a filesystem path.
        ids = self.headers.get_all("X-Source-ID", [])
        revisions = self.headers.get_all("X-Catalog-Revision", [])
        length = int(self.headers["Content-Length"])
        additions = self.headers.get_all("X-PDF-Add", [])
        adding = additions == ["1"]
        metadata = self.headers.get_all("X-PDF-Metadata", [])
        auto_title = self.headers.get_all("X-PDF-Auto-Title", [])
        formats = self.headers.get_all("X-Document-Format", [])
        requested_format = formats[0] if formats else "pdf"
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        if (len(ids) != 1 or not catalog_store.ID_RE.fullmatch(ids[0])
                or len(revisions) != 1 or not re.fullmatch(r"[0-9]{1,20}", revisions[0])
                or length <= 0 or self.headers.get("Content-Encoding")
                or (additions and not adding) or (adding and len(metadata) != 1)
                or (not adding and metadata)
                or (auto_title and (not adding or auto_title != ["1"]))
                or len(formats) > 1 or requested_format not in {"pdf", "epub"}):
            self._send_json(400, {"error": "invalid PDF upload metadata"})
            return
        sid, revision = ids[0], int(revisions[0])
        fields = {}
        if adding:
            try:
                if len(metadata[0]) > 8192:
                    raise ValueError
                fields = json.loads(unquote(metadata[0], errors="strict"))
                SOURCE_CATALOG._validate_fields(fields, catalog_store.PROFILE_FIELDS | {"source_url", "license", "update_policy"} | update_discovery_config.FIELDS,
                                                required={"title"})
                fields["format"] = requested_format
                update_discovery_config.settings(fields)
                if fields.get("source_url") and not pdf_source.valid_source_url(fields["source_url"]):
                    raise ValueError
            except (ValueError, UnicodeError):
                self._send_json(400, {"error": "invalid new PDF source metadata"})
                return
        # Read at most the verified request length before acquiring the shared lock.
        try:
            data = self.rfile.read(length)
        except OSError:
            self._send_json(400, {"error": "PDF upload interrupted"})
            return
        if len(data) != length:
            self._send_json(400, {"error": "PDF upload incomplete"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if revision != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if adding:
                    if record is not None or len(catalog["sources"]) >= catalog_store.MAX_ENTRIES:
                        raise ValueError("source ID already exists or catalog is full")
                elif record is None or not (self._pdf_record(record) if requested_format == "pdf" else self._epub_record(record)):
                    raise ValueError("source has no matching document upload adapter")
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish source before uploading")
                config = {} if adding else record["configuration"]
                size = config.get("expected_size_bytes") or None
                digest = config.get("sha256") or None
                if size and int(size) != length:
                    raise ValueError("Upload size does not match the catalog pin")
                if requested_format == "epub":
                    observed, extracted = epub_source.install_stream(sid, BytesIO(data), expected_size=length,
                                                                    expected_sha256=digest)
                    profile = fields if adding else record["profile"]
                    for key in ("author", "publisher", "language"):
                        if not profile.get(key) and extracted.get(key):
                            fields[key] = extracted[key]
                    if adding and auto_title and extracted.get("title"):
                        fields["title"] = extracted["title"]
                else:
                    observed = pdf_source.install_stream(sid, BytesIO(data), expected_size=length,
                                                         expected_sha256=digest)
                    if adding and auto_title:
                        fields["title"] = pdf_source.simple_document_title(data) or fields["title"]
                    if not (fields if adding else record["profile"]).get("language"):
                        language = pdf_language.detect(data)
                        if language:
                            fields["language"] = language
                note = "Uploaded by administrator; SHA-256 calculated locally, not independently publisher-verified."
                provenance = config.get("provenance", "").strip()
                if provenance:
                    provenance = provenance[:catalog_store.MAX_FIELD-len(note)-1] + " " + note
                else:
                    provenance = note
                fields.update(expected_size_bytes=str(observed["bytes"]), sha256=observed["sha256"],
                              provenance=provenance)
                updated = SOURCE_CATALOG.mutate("add" if adding else "edit", sid, fields, revision)
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "PDF storage or catalog I/O failed"})
                return
        self._send_json(200, {"accepted": True, "installed_verified": True, "revision": updated["revision"]})

    def _download_pdf(self, data):
        sid = data.get("source_id")
        adding = data.get("action") == "epub-add-download"
        required = {"source_id", "action", "revision"} | ({"fields", "auto_title"} if adding else set())
        if (set(data) != required or not isinstance(sid, str)
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "download requires a source ID and catalog revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        fields = {}
        if adding:
            try:
                if type(data["auto_title"]) is not bool:
                    raise ValueError("invalid automatic title flag")
                fields = dict(data["fields"]) if isinstance(data["fields"], dict) else None
                SOURCE_CATALOG._validate_fields(fields, catalog_store.PROFILE_FIELDS | {"source_url", "license", "update_policy"} | update_discovery_config.FIELDS,
                                                required={"title", "source_url"})
                if not pdf_source.valid_source_url(fields["source_url"]):
                    raise ValueError("A direct HTTPS EPUB URL is required")
                fields["format"] = "epub"
                update_discovery_config.settings(fields)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid new EPUB source metadata"})
                return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                is_epub = data["action"] in {"epub-download", "epub-add-download"}
                if adding:
                    if record is not None or len(catalog["sources"]) >= catalog_store.MAX_ENTRIES:
                        raise ValueError("source ID already exists or catalog is full")
                elif record is None or not (self._epub_record(record) if is_epub else self._pdf_record(record)):
                    raise ValueError("source has no matching document download adapter")
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish source before downloading")
                config = fields if adding else record["configuration"]
                result = _acquire_document(sid, record, config, data["revision"], is_epub,
                                           fields, adding=adding, auto_title=data.get("auto_title"))
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except FileNotFoundError:
                self._send_json(503, {"error": "PDF storage parent directory or catalog path is missing"})
                return
            except PermissionError:
                self._send_json(503, {"error": "PDF storage or catalog is not writable by the application"})
                return
            except OSError:
                self._send_json(503, {"error": "PDF storage or catalog I/O failed"})
                return
        self._send_json(200, result)

    def _delete_downloaded_files(self, data):
        sid = data.get("source_id")
        preview = "confirmation" not in data
        if (set(data) != ({"source_id", "action", "revision"} if preview else
                          {"source_id", "action", "revision", "confirmation"})
                or not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid)
                or not self._valid_revision(data["revision"])
                or not preview and (not isinstance(data["confirmation"], str)
                                    or not re.fullmatch("[a-f0-9]{64}", data["confirmation"]))):
            self._send_json(400, {"error": "Invalid file deletion request"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return

        unpublished = False
        removed = 0
        try:
            with publication.LOCK:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("Catalog changed; review the source again")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if not record:
                    raise ValueError("Source has no managed installation")
                kind = record["configuration"]["format"]
                if kind not in {"zim", "pmtiles", "pdf", "epub"}:
                    raise ValueError("Source has no supported downloaded files")
                if any(job["source_id"] == sid and job["state"] in acquisition_queue.PENDING
                       for job in acquisition_queue.status()):
                    raise ValueError("Finish or cancel this source’s batch download first")
                files = zim_jobs.downloaded_zim(sid) if kind in {"zim", "pmtiles"} else pdf_source.downloaded_documents(sid, kind)
                with files as (directory, opened):
                    enabled = publication.load().get(sid, False)
                    listed = [dict(filename=name, bytes=size) for name, _, size in opened]
                    identities = [zim_jobs.archive_identity(os.fstat(stream.fileno())) for _, stream, _ in opened]
                    encoded = json.dumps([sid, kind, enabled, listed, identities], sort_keys=True).encode()
                    token = hashlib.sha256(encoded).hexdigest()
                    if preview:
                        result = dict(files=listed, published=enabled, confirmation=token)
                    else:
                        if data["confirmation"] != token:
                            raise ValueError("Files or publication changed; review the deletion again")
                        if enabled:
                            publication.save(sid, False)
                            unpublished = True
                        try:
                            for name, stream, _ in opened:
                                pdf_source.unlink_managed_file(directory, stream, name)
                                removed += 1
                            if kind in {"zim", "pmtiles"}:
                                zim_jobs.forget_deleted_installation(sid)
                            os.fsync(directory)
                        finally:
                            if kind == "zim" and (unpublished or removed):
                                zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])
                        result = dict(deleted=removed)
        except (BlockingIOError, ValueError, OSError, sqlite3.Error, catalog_store.CatalogError) as exc:
            reason = (str(exc) if isinstance(exc, ValueError) else
                      "Another acquisition is active" if isinstance(exc, BlockingIOError) else
                      "Catalog, queue or storage operation failed")
            if unpublished or removed:
                prefix = "Source unpublished. " if unpublished else ""
                reason = f"{prefix}Deletion incomplete after removing {removed} file(s). {reason}"
            self._send_json(409, {"error": reason})
            return
        self._send_json(200, result)

    def _delete_staged_update(self, data):
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        sid, digest, revision = data.get("source_id"), data.get("candidate_sha256"), data.get("revision")
        if (set(data) != {"action", "source_id", "candidate_sha256", "revision", "confirm"}
                or data["confirm"] is not True or not self._valid_revision(revision)
                or not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            self._send_json(400, {"error": "Invalid staged update deletion request"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if revision != catalog["revision"]:
                    raise ValueError("Catalog changed. Reload and review the staged update.")
                record = next((row for row in catalog["sources"] if row["source_id"] == sid), None)
                if not record or not self._archive_record(record):
                    raise ValueError("No matching archive source")
                def restore_profile(live):
                    update_check_store.require_recheck(sid)
                    fields = {key: live["configuration"][key] for key in
                              ("source_url", "sha256", "expected_size_bytes")}
                    for digest_field in ("publisher_sha1", "publisher_md5"):
                        if digest_field in live["configuration"] or digest_field in record["configuration"]:
                            fields[digest_field] = live["configuration"].get(digest_field, "")
                    installed = kiwix_update_discovery.installed_identity_from_job(live, live["artifact"])
                    if installed:
                        fields["edition"] = installed["edition"]
                    if any((record["profile"] if key == "edition" else record["configuration"]).get(key, "") != value
                           for key, value in fields.items()):
                        SOURCE_CATALOG.mutate("edit", sid, fields, revision)

                result = zim_jobs.delete_verified_candidate(
                    sid, record["configuration"], digest, restore_profile=restore_profile)
                result["revision"] = SOURCE_CATALOG.get()["revision"]
            except (BlockingIOError, ValueError) as exc:
                self._send_json(409, {"error": str(exc) or "ZIM storage is busy"})
                return
            except OSError:
                self._send_json(503, {"error": "Staged update could not be removed safely"})
                return
        self._send_json(200, result)

    def _download_zim_update(self, data):
        """Start an operator-selected replacement using server-side update evidence."""
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        sid, digest = data.get("source_id"), data.get("candidate_sha256")
        revision = data.get("revision")
        if (set(data) != {"action", "source_id", "revision", "candidate_sha256"}
                or not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid)
                or not self._valid_revision(revision) or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{40}|[a-f0-9]{64}", digest)):
            self._send_json(400, {"error": "Invalid ZIM update request"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if revision != catalog["revision"]:
                    raise ValueError("Catalog changed. Reload and review the update again.")
                record = next((row for row in catalog["sources"] if row["source_id"] == sid), None)
                if not record or not self._archive_record(record):
                    raise ValueError("Source does not support archive updates")
                config = record["configuration"]
                if update_discovery_config.settings(config)["method"] not in {"kiwix", "archive", "builds"}:
                    raise ValueError("Source does not use a versioned archive update method")
                update = kiwix_update_discovery.cached_checked_source(sid)
                fields = _reviewed_update_fields(update, digest)
                replacement_config = config | fields
                current = zim_jobs.candidate_status(sid)
                if current and current["state"] != "activated":
                    raise ValueError("Finish or discard the current replacement first.")
                # The serving job remains authoritative until explicit activation.
                # A prepared candidate survives a failed attempt to start the worker.
                SOURCE_CATALOG.mutate("edit", sid, fields, revision)
                zim_jobs.prepare_candidate(sid, replacement_config, replace_activated=True)
                result = zim_jobs.start_candidate_acquisition(sid, replacement_config)
            except BlockingIOError:
                self._send_json(409, {"error": "This ZIM already has an active job or storage maintenance is running"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "ZIM update storage unavailable"})
                return
        self._send_json(202, result)

    def _start_zim(self, data):
        sid = data.get("source_id")
        expected_keys = ({"source_id", "action", "revision", "confirm_restart"}
                         if data.get("action") in {"zim-restart", "zim-replacement-activate", "zim-replacement-rollback", "zim-delete-installed"}
                         else {"source_id", "action", "revision"})
        if (set(data) != expected_keys
                or (data.get("action") in {"zim-restart", "zim-replacement-activate", "zim-replacement-rollback", "zim-delete-installed"} and data.get("confirm_restart") is not True)
                or not isinstance(sid, str)
                or data.get("action") not in {"zim-download", "zim-resume", "zim-pause", "zim-cancel", "zim-cancel-keep", "zim-restart", "zim-retry", "zim-revalidate", "zim-replacement-prepare", "zim-replacement-discard", "zim-replacement-activate", "zim-replacement-rollback", "zim-replacement-download", "zim-replacement-cancel", "zim-delete-installed"}
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "ZIM control requires a source ID and current revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((r for r in catalog["sources"] if r["source_id"] == sid), None)
                if not record or not select_lifecycle(sid, record["configuration"]).get("zim_download"):
                    raise ValueError("Source has no supported ZIM download URL")
                operation = data["action"]
                if publication.load().get(sid) and not operation.startswith("zim-replacement-"):
                    raise ValueError("Unpublish before downloading or deleting")
                if operation == "zim-delete-installed":
                    result = zim_jobs.delete_installed(sid)
                elif operation == "zim-replacement-cancel":
                    result = zim_jobs.cancel_candidate_acquisition(sid, record["configuration"])
                elif operation == "zim-replacement-download":
                    result = zim_jobs.start_candidate_acquisition(sid, record["configuration"])
                elif operation == "zim-replacement-activate":
                    result = zim_jobs.activate_candidate(sid, record["configuration"])
                elif operation == "zim-replacement-rollback":
                    result = zim_jobs.rollback_candidate(sid, record["configuration"])
                elif operation == "zim-replacement-prepare":
                    result = zim_jobs.prepare_candidate(sid, record["configuration"])
                elif operation == "zim-replacement-discard":
                    result = zim_jobs.discard_candidate(sid, record["configuration"])
                elif operation in {"zim-pause", "zim-cancel", "zim-cancel-keep"}:
                    result = zim_jobs.control(sid, record["configuration"], operation.removeprefix("zim-"))
                elif operation == "zim-revalidate":
                    result = zim_jobs.revalidate(sid, record["configuration"])
                elif operation == "zim-resume":
                    result = zim_jobs.start(sid, record["configuration"], resume=True)
                elif operation == "zim-restart":
                    result = zim_jobs.start(sid, record["configuration"], restart=True)
                elif operation == "zim-retry":
                    result = zim_jobs.start(sid, record["configuration"], retry=True)
                else:
                    result = zim_jobs.start(sid, record["configuration"])
            except BlockingIOError:
                self._send_json(409, {"error": "This ZIM already has an active job or storage maintenance is running"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except FileNotFoundError:
                self._send_json(503, {"error": "Required storage root or staging/library parent directory is missing. Check Storage settings."})
                return
            except PermissionError:
                self._send_json(503, {"error": "Application cannot write its ZIM job records or configured storage. Check storage permissions."})
                return
            except OSError:
                self._send_json(503, {"error": "ZIM job or storage unavailable; check Storage settings before retrying"})
                return
        if self._zim_record(record) and operation in {"zim-replacement-activate", "zim-replacement-rollback"}:
            # Kiwix reads a separate manifest and only acknowledges the
            # exact loaded book set. A failed activation must restore the
            # retained previous verified serving record, never delete it.
            if operation == "zim-replacement-activate":
                zim_refresh.schedule(
                    lambda: SOURCE_CATALOG.get()["sources"],
                    activated_source=sid,
                    activated_configuration=record["configuration"],
                )
            else:
                zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])
        self._send_json(202, result)

    def _refuse_acquisition(self, data):
        def refuse(status, error):
            self._send_json(status, {"accepted": False, "error": error})

        sid, action = data.get("source_id"), data.get("action")
        if not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid):
            return refuse(400, "invalid source_id")
        if not isinstance(action, str) or action not in ACTION_LABELS:
            return refuse(400, "unknown action")
        if set(data) != {"source_id", "action", "revision"} or not self._valid_revision(data["revision"]):
            return refuse(400, "action requires a catalog revision and accepts no acquisition metadata")
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                return refuse(503, "source catalog unavailable")
            if data["revision"] != catalog["revision"]:
                return refuse(409, "catalog revision changed")
            record = next((record for record in catalog["sources"] if record["source_id"] == sid), None)
            if record is None:
                return refuse(400, "unknown source_id: no configured profile")
            life = select_lifecycle(sid, record["configuration"])
            if not life["artifact"]:
                return refuse(409, life["acquisition_reason"] or "Use the ZIM Download action")
            snapshot = self._snapshot()
            matching = self._matched_snapshot(snapshot, {sid: life["artifact"]})
            if (source_busy(snapshot, sid) or (any(job["source_id"] == sid for job in snapshot["acquisitions"])
                                               and not matching["acquisitions"])):
                return refuse(409, "source has active or unresolved acquisition")
            # No admin-triggered acquisition is implemented, even for a trusted pin.
            return refuse(503, life["acquisition_reason"])

    @staticmethod
    def _guard_published_mutation(operation, sid, fields, revision):
        if operation not in ("remove", "edit", "add"):
            return
        try:
            enabled = publication.load().get(sid, False)
        except (OSError, ValueError) as exc:
            raise catalog_store.RevisionConflict("publication state unavailable; source mutation refused") from exc
        if not enabled:
            return
        if operation in ("remove", "add"):
            raise catalog_store.RevisionConflict("unpublish source before removing or re-adding it")
        guarded_fields = catalog_store.ARTIFACT_CONFIG_FIELDS
        if isinstance(fields, dict) and fields.keys() & guarded_fields:
            catalog = SOURCE_CATALOG.get()
            if catalog["revision"] != revision:
                raise catalog_store.RevisionConflict("catalog revision changed")
            record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
            if record:
                config = record["configuration"]
                proposed = config | fields
                if any(proposed.get(k, "") != config.get(k, "") for k in guarded_fields):
                    raise catalog_store.RevisionConflict("unpublish source before changing artifact configuration")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default per-request stderr logging; structured logging
        # is handled by the systemd unit.
        return


def main() -> None:
    """Start the admin API server."""
    config = security.load_config()
    server = ThreadingHTTPServer((config.host, config.port), AdminAPIHandler)
    server.axiom_config = config
    server.managed_acquisition_only = True
    queue_shutdown = acquisition_queue.start(_execute_batch_job, poll=_poll_automatic_updates)
    schedule_shutdown = kiwix_update_scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        queue_shutdown.set()
        schedule_shutdown.set()
        server.server_close()


if __name__ == "__main__":
    main()
