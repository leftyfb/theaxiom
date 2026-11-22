"""Offline tests for non-destructive PDF/EPUB representation checks."""
from unittest.mock import Mock

import pytest

from theaxiom import document_update_discovery as discovery

SID = "guide"
URL = "https://publisher.org/manual.pdf"
DIGEST = "a" * 64
ARTIFACT = {"filename": SID + "." + DIGEST + ".pdf",
            "source_url": URL, "sha256": DIGEST, "bytes": 100}
ETAG = '"installed-version"'


@pytest.mark.parametrize("kind", ["pdf", "epub"])
def test_document_check_requires_verified_installed_file_and_get_etag(monkeypatch, kind):
    adapter = discovery.pdf_source if kind == "pdf" else discovery.epub_source
    monkeypatch.setattr(adapter, "pin", lambda sid, config: ARTIFACT)
    monkeypatch.setattr(adapter, "ready", lambda artifact, **kwargs: True)
    monkeypatch.setattr(discovery.document_origin_evidence, "load",
                        lambda sid, k, url, digest: ETAG)
    saved = []
    monkeypatch.setattr(discovery.update_check_store, "save",
                        lambda *args: saved.append(args))
    probe = Mock(return_value={"source_url": URL, "status": 200,
                               "reachable": True, "etag": '"new-version"'})
    record = {"source_id": SID, "configuration": {"format": kind}}
    result = discovery.check_installed_document(record, probe=probe)
    assert result["state"] == "representation_changed"
    assert "update_available" not in str(result)
    probe.assert_called_once_with(URL, if_none_match=ETAG)
    assert saved[0][0:2] == (SID, DIGEST)


def test_conditional_304_confirms_same_representation(monkeypatch):
    monkeypatch.setattr(discovery.pdf_source, "pin", lambda *args: ARTIFACT)
    monkeypatch.setattr(discovery.pdf_source, "ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(discovery.document_origin_evidence, "load",
                        lambda *args: ETAG)
    result = discovery.check_installed_document(
        {"source_id": SID, "configuration": {"format": "pdf"}},
        probe=lambda url, **kwargs: {"source_url": url, "reachable": True,
                                     "status": 304, "etag": ""}, persist=False)
    assert result["state"] == "representation_unchanged"


def test_unverified_or_missing_etag_never_performs_network_request(monkeypatch):
    monkeypatch.setattr(discovery.pdf_source, "pin", lambda *args: ARTIFACT)
    monkeypatch.setattr(discovery.pdf_source, "ready", lambda *args, **kwargs: False)
    forbidden = lambda *args, **kwargs: pytest.fail("Unverified file contacted publisher")
    rec = {"source_id": SID, "configuration": {"format": "pdf"}}
    assert discovery.check_installed_document(rec, probe=forbidden)["state"] == "unable_to_determine"
    monkeypatch.setattr(discovery.pdf_source, "ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(discovery.document_origin_evidence, "load",
                        lambda *args: None)
    assert discovery.check_installed_document(rec, probe=forbidden)["state"] == "unable_to_determine"


def test_wrong_format_and_bad_adapter_fail_closed_without_network():
    for record in (None, {}, {"source_id": SID, "configuration": {"format": "other"}},
                   {"source_id": SID, "configuration": None}):
        assert discovery.check_installed_document(record)["state"] == "unable_to_determine"
