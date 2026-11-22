"""Kiwix reader startup must acknowledge the exact published archive set."""
from io import BytesIO
import uuid
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest

from theaxiom import zim_catalog, zim_reader_probe, zim_source, zim_jobs, publication
from test_zim_source import CONFIG, disk, fixture_bytes


def records():
    return [{"source_id": "ready", "configuration": CONFIG,
             "profile": {"title": "Preparedness", "language": "English"}}]


def installed(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    publication.save("ready", True)
    return records()


def test_startup_acknowledges_only_exact_published_content(disk, tmp_path):
    rec = installed(disk)
    zim_catalog.write_manifest(rec)
    book_name = zim_catalog.content_name(zim_catalog.verified_books(rec)[0])
    marker = tmp_path / "manifest.sha256"
    with pytest.raises(ValueError, match="unexpected"):
        zim_reader_probe.acknowledge(rec, path=marker, book_ids={"another"})
    assert not marker.exists()
    zim_reader_probe.acknowledge(rec, path=marker, book_ids={book_name})
    assert zim_catalog.active_manifest_matches(rec, acknowledgement=marker)
    publication.save("ready", False)
    assert not zim_catalog.active_manifest_matches(rec, acknowledgement=marker)


def test_no_ack_when_manifest_or_installed_archives_disagree(disk, tmp_path):
    rec = installed(disk)
    zim_catalog.write_manifest(rec)
    marker = tmp_path / "manifest.sha256"
    (zim_catalog.manifest_path()).write_text("changed")
    with pytest.raises(ValueError, match="differs"):
        zim_reader_probe.acknowledge(rec, path=marker, book_ids={"ready"})
    zim_catalog.write_manifest(rec)
    publication.save("ready", False)
    with pytest.raises(ValueError, match="No verified"):
        zim_reader_probe.acknowledge(rec, path=marker, book_ids={"ready"})
    assert not marker.exists()


def test_opds_book_identity_rejects_ambiguous_duplicates_and_external_links():
    def read(xml):
        with patch.object(zim_reader_probe.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = xml.encode()
            return zim_reader_probe.listed_book_ids()
    slug = "ready." + "a"*64
    good = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/' + slug + '"/></entry></feed>'
    assert read(good) == {slug}
    for xml in (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://evil.example/zim/content/' + slug + '"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/' + slug + '"/><link href="/zim/content/other.' + "b"*64 + '"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/' + slug + '"/></entry><entry><link href="/zim/content/' + slug + '"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/' + slug + '%2funsafe"/></entry></feed>',
    ):
        with pytest.raises(ValueError):
            read(xml)


def test_opds_accepts_only_exact_trusted_legacy_wikipedia_identity():
    from pathlib import Path
    from theaxiom.source_catalog import WIKIPEDIA_SOURCE
    legacy = Path(WIKIPEDIA_SOURCE["filename"]).stem
    def read(book_id):
        xml = (f'<feed xmlns="http://www.w3.org/2005/Atom">'
               f'<entry><link href="/zim/content/{book_id}"/></entry></feed>')
        with patch.object(zim_reader_probe.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = xml.encode()
            return zim_reader_probe.listed_book_ids()
    assert read(legacy) == {legacy}
    for invalid in (legacy.upper(), legacy + ".zim", "wikipedia_en_all_maxi_2026-07",
                    "wikipedia_en_all_maxi_2026-08%2fother"):
        with pytest.raises(ValueError, match="Ambiguous"):
            read(invalid)


def test_shared_manifest_includes_only_verified_published_wikipedia(disk):
    from theaxiom import source_catalog, acquisition_monitor
    trusted = source_catalog.WIKIPEDIA_SOURCE
    evidence = {"state": "completed", "verified": True, "active": False,
                "filename": trusted["filename"], "sha256": trusted["sha256"],
                "expected_bytes": trusted["bytes"]}
    # Sparse fixture mimics a large, verified archive without allocating its bytes.
    wikipedia_path = disk / "library" / "zim" / trusted["filename"]
    wikipedia_path.parent.mkdir(parents=True, exist_ok=True)
    with wikipedia_path.open("wb") as stream:
        stream.write(fixture_bytes()[:24])
        stream.truncate(trusted["bytes"])
    with patch.object(acquisition_monitor, "collect", return_value=evidence) as probe:
        publication.save("wikipedia-en", True)
        books = zim_catalog.verified_books([])
        assert [book["id"] for book in books] == [str(uuid.UUID(int=0))]
        assert books[0]["path"].endswith("/library/zim/" + trusted["filename"])
        assert zim_catalog.content_name(books[0]) == trusted["filename"][:-4]
        assert probe.call_count == 1
        publication.save("wikipedia-en", False)
        assert zim_catalog.verified_books([]) == []
    with patch.object(acquisition_monitor, "collect", return_value={**evidence, "verified": False}):
        publication.save("wikipedia-en", True)
        assert zim_catalog.verified_books([]) == []


@pytest.mark.parametrize(("error", "category"), [
    (OSError("/secret/private/archive.zim missing"), "listener-or-filesystem-unavailable"),
    (ValueError("Kiwix reader not ready"), "listener-not-ready"),
    (ValueError("Ambiguous Kiwix book identity"), "opds-invalid"),
    (ValueError("Kiwix listener is serving an unexpected set of archives"), "opds-book-set-mismatch"),
    (ValueError("Publication changed during Kiwix startup"), "publication-state-changed"),
    (ValueError("No verified published ZIMs to serve"), "no-published-archives"),
    (ValueError("secret-token=do-not-print"), "readiness-validation-failed"),
])
def test_readiness_failures_are_bounded_and_redacted(error, category):
    assert zim_reader_probe._failure_category(error) == category
    assert "secret" not in category


def test_readiness_timeout_logs_only_safe_category(monkeypatch, capsys):
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.return_value = {"sources": []}
    monkeypatch.setattr(zim_reader_probe.catalog_store, "CatalogStore", lambda path: store)
    monkeypatch.setattr(zim_reader_probe, "acknowledge",
                        lambda records: (_ for _ in ()).throw(OSError("/private/path token=SECRET")))
    ticks = iter((0, 21))
    monkeypatch.setattr(zim_reader_probe.time, "monotonic", lambda: next(ticks))
    assert zim_reader_probe.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "last_failure=listener-or-filesystem-unavailable" in captured.err
    assert "1 attempts" in captured.err
    assert "/private/" not in captured.err
    assert "SECRET" not in captured.err


def test_readiness_transient_failures_do_not_log(monkeypatch, capsys):
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.return_value = {"sources": []}
    monkeypatch.setattr(zim_reader_probe.catalog_store, "CatalogStore", lambda path: store)
    attempts = []
    def handshake(records):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("Kiwix reader not ready")
    monkeypatch.setattr(zim_reader_probe, "acknowledge", handshake)
    monkeypatch.setattr(zim_reader_probe.time, "monotonic", lambda: 0)
    monkeypatch.setattr(zim_reader_probe.time, "sleep", lambda delay: None)
    assert zim_reader_probe.main() == 0
    assert len(attempts) == 2
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("status", "body", "expected"), [
    (503, b"temporary outage", "Kiwix reader not ready"),
    (200, b"<!DOCTYPE feed><feed/>", "Invalid Kiwix catalog response"),
    (200, b"<!ENTITY leaked SYSTEM 'file:///private/key'><feed/>", "Invalid Kiwix catalog response"),
    (200, b"x" * (zim_reader_probe.MAX_RESPONSE + 1), "Invalid Kiwix catalog response"),
    (200, b"<feed", "Could not validate Kiwix OPDS catalog"),
    (200, b'<feed xmlns="http://www.w3.org/2005/Atom"/>', "Kiwix catalog size is unexpected"),
])
def test_opds_readiness_rejects_untrusted_or_unavailable_responses(status, body, expected):
    """An unhealthy sidecar must never be mistaken for a ready reader."""
    with patch.object(zim_reader_probe.http.client, "HTTPConnection") as factory:
        connection = factory.return_value
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = body
        with pytest.raises(ValueError, match=expected):
            zim_reader_probe.listed_book_ids()
        connection.close.assert_called_once()


def test_opds_readiness_rejects_network_failure_and_closes_connection():
    with patch.object(zim_reader_probe.http.client, "HTTPConnection") as factory:
        connection = factory.return_value
        connection.request.side_effect = TimeoutError("private-network-target")
        with pytest.raises(TimeoutError):
            zim_reader_probe.listed_book_ids()
        connection.close.assert_called_once()
        assert zim_reader_probe._failure_category(
            TimeoutError("private-network-target")) == "listener-or-filesystem-unavailable"
