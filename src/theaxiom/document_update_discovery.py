"""Conservative PDF/EPUB remote representation checks for verified local files.

A changed strong ETag is a *representation* change, never proof of an
authorized newer edition. No acquisition, update or deletion is possible here.
"""
from __future__ import annotations

from . import (document_origin_evidence, epub_source, freshness_probe,
               pdf_source, pdf_transport, update_check_store, update_discovery_config)

UNKNOWN = {"state": "unable_to_determine",
           "evidence": "no comparable installed document origin"}


def check_installed_document(record, *, probe=None, persist=True):
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
            return dict(UNKNOWN)
        url = artifact["source_url"]
        digest = artifact["sha256"]
        endpoint = selected["endpoint"]
        etag = document_origin_evidence.load(sid, kind, url, digest) if endpoint == url else None
        cache_id = update_discovery_config.cache_identity(config, digest)
        if not etag:
            if not config.get("update_endpoint"):
                return dict(UNKNOWN)
            diagnostic = update_discovery_config.probe_endpoint(selected)
            result = dict(UNKNOWN, evidence=diagnostic["evidence"] if diagnostic["state"] == "failed"
                          else "Endpoint reachable; no capture-time ETag for this exact URL")
            if persist:
                update_check_store.save(sid, cache_id, result)
            return result
        probe = pdf_transport.probe_document_metadata if probe is None else probe
        try:
            response = probe(url, if_none_match=etag)
            result = freshness_probe.classify_document_origin({
                "verified": True, "source_url": url,
                "sha256": digest, "etag": etag,
            }, response)
        except (OSError, ValueError):
            result = dict(UNKNOWN, evidence="Publisher HEAD failed HTTPS, public address, response, or timeout checks")
        if persist:
            update_check_store.save(sid, cache_id, result)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)
