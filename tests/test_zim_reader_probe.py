"""Kiwix reader startup must acknowledge the exact published archive set."""
from io import BytesIO
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
    marker = tmp_path / "manifest.sha256"
    with pytest.raises(ValueError, match="unexpected"):
        zim_reader_probe.acknowledge(rec, path=marker, book_ids={"another"})
    assert not marker.exists()
    zim_reader_probe.acknowledge(rec, path=marker, book_ids={"ready"})
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
    good = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/ready"/></entry></feed>'
    assert read(good) == {"ready"}
    for xml in (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://evil.example/zim/content/ready"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/ready"/><link href="/zim/content/other"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/ready"/></entry><entry><link href="/zim/content/ready"/></entry></feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="/zim/content/ready%2funsafe"/></entry></feed>',
    ):
        with pytest.raises(ValueError):
            read(xml)
