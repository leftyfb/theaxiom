"""Conservative PDF/EPUB remote representation checks for verified local files.

A changed strong ETag is a *representation* change, never proof of an
authorized newer edition. No acquisition, update or deletion is possible here.
"""
from __future__ import annotations

from . import (document_replacement, epub_source, freshness_probe,
               pdf_source, pdf_transport, update_check_store, update_discovery_config, update_diagnostics)

UNKNOWN = {"state": "unable_to_determine",
           "evidence": "no comparable installed document origin"}


def check_installed_document(record, *, probe=None, persist=True, installed_only=False):
    if not isinstance(record, dict) or not isinstance(record.get("configuration"), dict):
        return dict(UNKNOWN)
    sid = record.get("source_id")
    config = record["configuration"]
    kind = config.get("format")
    if kind not in {"pdf", "epub"}:
        return dict(UNKNOWN)
    try:
        selected = update_discovery_config.settings(config)
        if selected["method"] != "head":
            return dict(UNKNOWN)
        # Use the acquisition-time validator bound to the installed digest. The
        # metadata check confirms file presence without rehashing the archive.
        if kind == "pdf":
            artifact = pdf_source.pin(sid, config)
            verified = pdf_source.ready(artifact)
        else:
            artifact = epub_source.pin(sid, config)
            verified = epub_source.ready(artifact, verify=False)
        if not verified:
            return None if installed_only else dict(UNKNOWN)
        url = artifact["source_url"]
        digest = artifact["sha256"]
        endpoint = selected["endpoint"]
        etag = document_replacement.validator(record, artifact) if endpoint == url else None
        recheck_token = update_check_store.pending_recheck_token(sid)
        cache_id = update_discovery_config.cache_identity(config, digest)
        if not etag:
            result = dict(UNKNOWN, evidence="No strong ETag was saved for the installed file and this URL.")
            if config.get("update_endpoint"):
                update_diagnostics.mark("document_head")
                diagnostic = update_discovery_config.probe_endpoint(selected)
                result["evidence"] = (diagnostic["evidence"] if diagnostic["state"] == "failed"
                                      else "Endpoint reachable; no capture-time ETag for this exact URL")
            if persist:
                update_check_store.save(sid, cache_id, result, recheck_token=recheck_token)
            return result
        probe = pdf_transport.probe_document_metadata if probe is None else probe
        try:
            update_diagnostics.mark("document_head")
            response = probe(url, if_none_match=etag)
            update_diagnostics.mark("document_compare" if response.get("reachable") else "document_head",
                                    http_status=response.get("status"))
            result = freshness_probe.classify_document_origin({
                "verified": True, "source_url": url,
                "sha256": digest, "etag": etag,
            }, response)
            result = document_replacement.evidence(record, result, response, etag, artifact)
            status = response.get("status")
            if response.get("head_status"):
                prefix = f'HEAD returned HTTP {response["head_status"]}; conditional range GET returned HTTP {status}. '
                result["evidence"] = prefix + (result["evidence"] if response.get("reachable")
                                               else "Publisher metadata unavailable.")
            elif not response.get("reachable") and type(status) is int:
                result["evidence"] = f"Publisher HEAD returned HTTP {status}; update status is unknown."
        except (OSError, ValueError):
            result = dict(UNKNOWN, evidence="Publisher metadata request failed HTTPS, public address, response, or timeout checks")
        if persist:
            update_check_store.save(sid, cache_id, result, recheck_token=recheck_token)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)
