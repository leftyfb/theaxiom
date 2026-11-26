"""Source-scoped metadata discovery settings and endpoint diagnostics."""
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from . import kiwix_metadata_transport as kiwix, kiwix_opds, pdf_source, pdf_transport, versioned_archive_catalog as archives

from .catalog_store import DISCOVERY_FIELDS as FIELDS
OPDS = f"https://{kiwix.CATALOG_HOST}{kiwix.CATALOG_PATH}"


def settings(config):
    kind = config.get("format")
    download = config.get("source_url", "")
    method = config.get("update_method") or "auto"
    endpoint = config.get("update_endpoint", "")
    periodic = config.get("update_periodic", "")
    archive = archives.internet_archive_identity(download) if isinstance(download, str) else None
    if method not in {"auto", "kiwix", "head", "archive", "manual"} or periodic not in {"", "yes", "no"}:
        raise ValueError("Invalid update discovery settings")
    if method == "auto":
        if kind == "zim" and (not download or urlsplit(download).hostname in kiwix_opds.DOWNLOAD_HOSTS):
            resolved = "kiwix"
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
    # Explicit methods opt in. Existing sources retain their previous schedule.
    enabled = resolved != "manual" and (periodic == "yes" or not periodic and method == "auto")
    return dict(method=resolved, endpoint=endpoint, periodic=enabled)


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
