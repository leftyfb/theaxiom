"""Conservative PDF/EPUB remote representation checks for verified local files.

A changed strong ETag is a *representation* change, never proof of an
authorized newer edition. No acquisition, update or deletion is possible here.
"""
from __future__ import annotations

from . import (document_origin_evidence, epub_source, freshness_probe,
               pdf_source, pdf_transport, update_check_store)

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
        if kind == "pdf":
            artifact = pdf_source.pin(sid, config)
            verified = pdf_source.ready(artifact, verify=True)
        else:
            artifact = epub_source.pin(sid, config)
            verified = epub_source.ready(artifact)
        if not verified:
            return dict(UNKNOWN)
        url = artifact["source_url"]
        digest = artifact["sha256"]
        etag = document_origin_evidence.load(sid, kind, url, digest)
        if not etag:
            return dict(UNKNOWN)
        probe = pdf_transport.probe_document_metadata if probe is None else probe
        response = probe(url, if_none_match=etag)
        result = freshness_probe.classify_document_origin({
            "verified": True, "source_url": url,
            "sha256": digest, "etag": etag,
        }, response)
        if persist:
            update_check_store.save(sid, digest, result)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)
