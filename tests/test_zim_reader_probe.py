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
