"""Source-scoped metadata discovery settings and endpoint diagnostics."""
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import re
from urllib.parse import urlsplit

from . import kiwix_metadata_transport as kiwix, kiwix_opds, pdf_source, pdf_transport, versioned_archive_catalog as archives

from .catalog_store import DISCOVERY_FIELDS as FIELDS  # noqa: F401 - public alias
OPDS = f"https://{kiwix.CATALOG_HOST}{kiwix.CATALOG_PATH}"
UNATTENDED = {"automatic", "scheduled"}
POLICIES = UNATTENDED | {"notify", "disabled"}


def policy(config, method=None):
    """Legacy records never imply permission to acquire content."""
    selected = config.get("update_policy") or (
        "disabled" if config.get("update_method") not in (None, "", "auto") else "notify")
    if selected == "recommend" and config.get("format") in {"pdf", "epub"}:
        selected = "disabled"  # pending capability; never authorizes unattended work
    if selected not in POLICIES:
        raise ValueError("Invalid update policy")
    # Legacy explicit opt-outs always win, including contradictory unattended
    # settings. A new explicit policy no longer needs a second switch.
    if "update_periodic" in config:
        legacy = config.get("update_periodic")
        if legacy not in {"", "yes", "no"}:
            raise ValueError("Invalid update discovery settings")
        if legacy == "no" or (not legacy and config.get("update_method") not in (None, "", "auto")):
            selected = "disabled"
        elif legacy == "yes" and not config.get("update_policy"):
            selected = "notify"
    if selected not in POLICIES:
        raise ValueError("Invalid update policy")
    if selected in UNATTENDED:
        kind = config.get("format")
        if kind in {"pdf", "epub"}:
            url = config.get("source_url", "")
            if (method not in {None, "head"} or not pdf_source.valid_source_url(url)
                    or config.get("update_endpoint", "") not in ("", url)):
                raise ValueError("Unattended document updates require discovery at the exact configured HTTPS URL")
        elif kind not in {"zim", "pmtiles"} or method not in {None, "kiwix", "archive", "builds"}:
            raise ValueError("Unattended updates require supported publisher discovery")
    start, end = (config.get(key, "") for key in ("update_window_start", "update_window_end"))
    if selected == "scheduled" or start or end:
        if not all(isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
                   for value in (start, end)) or start == end:
            raise ValueError("Use two different local maintenance window times (HH:MM)")
    return selected, start, end


def window_open(config, local_now):
    """Check an inclusive start and exclusive end in server-local wall time."""
    selected, start, end = policy(config)
    if selected != "scheduled":
        return selected == "automatic"
    start, end = time.fromisoformat(start), time.fromisoformat(end)
    current = local_now.time()
    return start <= current < end if start < end else current >= start or current < end


def window_start(config, now):
    """Opening of the current window, or the next one if it is closed."""
    _, start, end = policy(config)
    start, end = time.fromisoformat(start), time.fromisoformat(end)
    day = now.date()
    if window_open(config, now):
        if start > end and now.time() < end:
            day -= timedelta(days=1)
    elif now.time() >= start:
        day += timedelta(days=1)
    opening = datetime.combine(day, start, now.tzinfo)
    return datetime.fromtimestamp(opening.timestamp(), now.tzinfo)


def current_unattended_evidence(config, now, evidence):
    if evidence.get("state") in (None, "unable_to_determine") or evidence.get("stale"):
        return False
    return (policy(config)[0] != "scheduled"
            or evidence.get("checked_at", 0) >= window_start(config, now).timestamp())


def settings(config):
    kind = config.get("format")
    download = config.get("source_url", "")
    method = config.get("update_method") or "auto"
    endpoint = config.get("update_endpoint", "")
    periodic = config.get("update_periodic", "")
    archive = archives.internet_archive_identity(download) if isinstance(download, str) else None
    map_build = archives.protomaps_identity(download)
    if method not in {"auto", "kiwix", "head", "archive", "builds", "manual"} or periodic not in {"", "yes", "no"}:
        raise ValueError("Invalid update discovery settings")
    if method == "auto":
        if kind == "zim" and (not download or urlsplit(download).hostname in kiwix_opds.DOWNLOAD_HOSTS):
            resolved = "kiwix"
        elif kind == "pmtiles" and map_build:
            resolved = "builds"
        elif kind == "zim" and archive:
            resolved = "archive"
        elif kind in {"pdf", "epub"} and pdf_source.valid_source_url(endpoint or download):
            resolved = "head"
        else:
            resolved = "manual"
    else:
        resolved = method
    if ((resolved == "kiwix" and kind != "zim")
            or (resolved == "head" and kind not in {"pdf", "epub"})):
        raise ValueError("Discovery method is incompatible with the source format")
    if resolved == "kiwix":
        if endpoint and endpoint != OPDS:
            raise ValueError("Select the approved Kiwix OPDS catalog")
        endpoint = OPDS
    elif resolved == "builds":
        if kind != "pmtiles" or not map_build or (endpoint and endpoint != archives.PROTOMAPS_ENDPOINT):
            raise ValueError("Map build discovery requires a matching dated build URL")
        endpoint = archives.PROTOMAPS_ENDPOINT
    elif resolved == "archive":
        if kind != "zim" or not archive or (endpoint and endpoint != archive["endpoint"]):
            raise ValueError("Archive discovery requires the matching dated Internet Archive item")
        endpoint = archive["endpoint"]
    elif resolved == "head":
        endpoint = endpoint or download
        if not pdf_source.valid_source_url(endpoint):
            raise ValueError("Document discovery requires a plain HTTPS endpoint")
    elif endpoint or periodic == "yes":
        raise ValueError("Manual discovery has no endpoint or periodic checks")
    selected, _, _ = policy(config, resolved)
    enabled = resolved != "manual" and selected != "disabled"
    return dict(method=resolved, endpoint=endpoint, periodic=enabled)


def migrate_periodic(config):
    """Canonicalize legacy profiles without enabling opted-out transfers."""
    if "update_periodic" not in config:
        return config
    selected, _, _ = policy(config)
    return {key: value for key, value in config.items() if key != "update_periodic"} | {"update_policy": selected}


def cache_identity(config, digest):
    selected = settings(config)
    endpoint = selected["endpoint"]
    if selected["method"] == "head" and endpoint != config.get("source_url"):
        return sha256(f"{digest}\0{endpoint}".encode()).hexdigest()
    return digest


def probe_endpoint(selected):
    method, endpoint = selected["method"], selected["endpoint"]
    result = {"checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        if method == "kiwix":
            raw = kiwix.fetch_metadata(endpoint, kind="catalog")
            kiwix_opds.parse_acquisition_feed(raw)
            return dict(result, state="reachable", evidence="Approved Kiwix OPDS catalog returned valid metadata")
        if method == "builds":
            archives.protomaps_latest(archives.fetch_metadata(endpoint))
            return dict(result, state="reachable", evidence="Map build catalog returned valid metadata")
        if method == "archive":
            import json
            metadata = json.loads(archives.fetch_metadata(endpoint))
            if metadata["metadata"]["identifier"] != endpoint.rsplit("/", 1)[-1] or not isinstance(metadata["files"], list):
                raise ValueError("Archive metadata identity mismatch")
            return dict(result, state="reachable", evidence="Internet Archive item returned valid metadata")
        if method == "head":
            response = pdf_transport.probe_document_metadata(endpoint)
            if not response["reachable"]:
                return dict(result, state="failed", evidence=f'Publisher returned HTTP {response["status"]}')
            evidence = ("HTTPS HEAD returned a strong ETag" if response["etag"]
                        else "HTTPS HEAD succeeded without a strong ETag")
            return dict(result, state="reachable", evidence=evidence)
    except (ValueError, OSError, KeyError, TypeError):
        return dict(result, state="failed",
                    evidence="Endpoint failed HTTPS, public address, response, or timeout checks")
    return dict(result, state="unsupported", evidence="Manual discovery has no metadata endpoint")
