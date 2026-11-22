"""Generic ZIM manifest requires publication intent and installed verified bytes."""
import xml.etree.ElementTree as ET
import uuid

from theaxiom import publication, storage_settings, zim_catalog, zim_jobs, zim_source
from test_zim_source import CONFIG, disk, fixture_bytes
from io import BytesIO


def record(sid, title="Ready & <Prepared>", config=None, language="English"):
    return {"source_id": sid, "configuration": config or CONFIG,
            "profile": {"title": title, "language": language}}


def test_unpublished_and_missing_archives_are_excluded(disk):
    zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    assert zim_catalog.verified_books([record("ready")], {}) == []
    assert zim_catalog.verified_books([record("ready")], {"ready": True}) == []
    xml = zim_catalog.render_xml([record("ready")], {"ready": True})
    assert ET.fromstring(xml).find("book") is None


def test_only_matching_completed_job_is_included_and_xml_escaped(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    records = [record("ready"), record("missing")]
    xml = zim_catalog.render_xml(records, {"ready": True, "missing": True})
    books = ET.fromstring(xml).findall("book")
    assert len(books) == 1
    assert books[0].attrib == {
        "id": str(uuid.UUID(int=0)), "path": str(disk / "library/zim" / artifact["filename"]),
        "title": "Ready & <Prepared>", "language": "eng"}
    assert b"Ready &amp; &lt;Prepared&gt;" in xml
    assert zim_catalog.render_xml(list(reversed(records)), {"ready": True, "missing": True}) == xml


def test_edited_source_configuration_keeps_verified_installed_book_until_activation(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    wrong = record("ready", config=CONFIG | {"source_url": "https://download.kiwix.org/other.zim"})
    assert zim_catalog.verified_books([wrong], {"ready": True})[0]["path"] == str(
        disk / "library/zim" / artifact["filename"]
    )
    path = disk / "library/zim" / artifact["filename"]
    path.chmod(0o666)
    assert zim_catalog.verified_books([record("ready")], {"ready": True}) == []


def test_world_writable_directory_still_supported_without_permission_change(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    directory = disk / "library/zim"
    directory.chmod(0o777)
    manifest = zim_catalog.write_manifest([record("ready")], {"ready": True})
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o777
    assert ET.fromstring(manifest.read_bytes()).find("book").get("id") == str(uuid.UUID(int=0))


def test_unpublished_manifest_drops_previous_book_without_removing_archive(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    zim_catalog.write_manifest([record("ready")], {"ready": True})
    path = zim_catalog.write_manifest([record("ready")], {"ready": False})
    assert ET.fromstring(path.read_bytes()).find("book") is None
    assert (disk / "library/zim" / artifact["filename"]).exists()


def test_missing_mount_fails_closed_without_existing_manifest_overwrite(disk):
    from unittest.mock import patch
    import pytest
    marker = zim_catalog.manifest_path()
    marker.write_bytes(b"existing trusted snapshot")
    with patch.object(zim_catalog.pdf_source, "_directory", side_effect=ValueError("mount unavailable")):
        with pytest.raises(ValueError, match="mount unavailable"):
            zim_catalog.write_manifest([record("ready")], {"ready": True})
    assert marker.read_bytes() == b"existing trusted snapshot"


def test_manifest_write_is_repeatable_and_not_auto_activated(disk):
    # Writing a private catalog cannot turn on a publication flag by itself.
    first = zim_catalog.write_manifest([], {})
    before = first.read_bytes()
    second = zim_catalog.write_manifest([], {})
    assert second == first and second.read_bytes() == before
    assert publication.load() == {}


def test_completed_but_unverified_job_never_enters_manifest(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": False, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    assert zim_catalog.verified_books([record("ready")], {"ready": True}) == []


def test_runtime_reader_acknowledgement_must_match_current_publication(disk, tmp_path):
    import hashlib
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    records = [record("ready")]
    filename = zim_catalog.write_manifest(records, {"ready": True})
    checkpoint = tmp_path / "manifest.sha256"
    assert not zim_catalog.active_manifest_matches(records, {"ready": True}, acknowledgement=checkpoint)
    checkpoint.write_text(hashlib.sha256(filename.read_bytes()).hexdigest() + "\n")
    assert zim_catalog.active_manifest_matches(records, {"ready": True}, acknowledgement=checkpoint)
    assert not zim_catalog.active_manifest_matches(records, {"ready": False}, acknowledgement=checkpoint)
    assert not zim_catalog.active_manifest_matches([record("ready", title="Changed")],
                                                    {"ready": True}, acknowledgement=checkpoint)
    checkpoint.write_text("unverified\n")
    assert not zim_catalog.active_manifest_matches(records, {"ready": True}, acknowledgement=checkpoint)


def test_generic_zim_publication_is_pending_until_acknowledged_reader(disk, tmp_path):
    from unittest.mock import patch
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    rec = record("ready", config=CONFIG | {"license": "Approved"})
    zim_catalog.write_manifest([rec], {"ready": True})
    publication.save("ready", True)
    status = zim_catalog.status(rec, [rec])
    assert status["enabled"] and status["verified"]
    assert not status["published"] and not status["serving"]
    assert "Waiting for Kiwix to acknowledge" in status["publication_reason"]
    with patch.object(zim_catalog, "active_manifest_matches", return_value=True):
        with patch.object(publication, "reader_location", return_value="/zim/viewer#ready/"):
            status = zim_catalog.status(rec, [rec])
            assert status["published"] and status["entry_path"] == "/read/zim/ready"
    assert not zim_catalog.status(record("other"), [rec])["published"]


def test_reader_content_auth_requires_specific_published_book(disk):
    from unittest.mock import patch
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    rec = record("ready")
    publication.save("ready", True)
    name = zim_catalog.content_name({"path": artifact["filename"]})
    assert name == "ready." + artifact["sha256"]
    import hashlib
    manifest = zim_catalog.write_manifest([rec], {"ready": True})
    checkpoint = disk / "reader-ack.sha256"
    checkpoint.write_text(hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n")
    with patch.object(zim_catalog, "RUNTIME_ACK", checkpoint):
        assert zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "/article")
        assert zim_catalog.authorize_reader_uri([rec], "/zim/viewer")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/other/article")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "%2Fother/article")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/../ready")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name.upper() + "/article")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + ".extra/article")
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "%2Farticle")
        assert not zim_catalog.authorize_reader_uri([rec], "/admin")
        publication.save("ready", False)
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "/article")


# Kiwix exposes a filename-derived content ID rather than the catalog source ID.

def test_kiwix_xml_version_and_installed_file_slug(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    books = zim_catalog.verified_books([record("ready")], {"ready": True})
    assert zim_catalog.content_name(books[0]) == "ready." + artifact["sha256"]
    assert ET.fromstring(zim_catalog.render_xml([record("ready")], {"ready": True})).get("version") == "20110515"


def test_manifest_mtime_increases_with_fast_atomic_updates(disk, monkeypatch):
    # Kiwix's watch loop uses integer-second timestamps and strictly >.
    # Simulate multiple publication changes within the same wall second.
    monkeypatch.setattr(zim_catalog.time, "time", lambda: 1000.0)
    path = zim_catalog.write_manifest([], {})
    first = path.stat().st_mtime_ns
    path = zim_catalog.write_manifest([], {})
    second = path.stat().st_mtime_ns
    path = zim_catalog.write_manifest([], {})
    third = path.stat().st_mtime_ns
    assert second >= first + 1_000_000_000
    assert third >= second + 1_000_000_000


def test_reader_auth_does_not_reverify_archives_per_request(disk):
    import hashlib
    from unittest.mock import patch
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    rec = record("ready")
    publication.save("ready", True)
    manifest = zim_catalog.write_manifest([rec], {"ready": True})
    ack = disk / "fast-reader-ack.sha256"
    ack.write_text(hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n")
    name = "ready." + artifact["sha256"]
    with (patch.object(zim_catalog, "RUNTIME_ACK", ack),
          patch.object(zim_catalog, "verified_books", side_effect=AssertionError("expensive verification")),
          patch.object(zim_catalog, "render_xml", side_effect=AssertionError("expensive manifest generation"))):
        assert zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "/article")
        publication.save("ready", False)
        assert not zim_catalog.authorize_reader_uri([rec], "/zim/content/" + name + "/article")


def test_reader_status_explains_activation_stage(disk):
    from unittest.mock import patch
    config = CONFIG | {"license": "CC-BY"}
    rec = record("ready", config=config)
    publication.save("ready", True)
    missing = zim_catalog.status(rec, [rec])
    assert missing["published"] is False
    assert missing["publication_reason"] == "Download and verify the archive before publishing"
    artifact = zim_source.install_stream("ready", config, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(config),
                   "artifact": artifact})
    with patch.object(zim_catalog, "active_manifest_matches", return_value=False):
        waiting = zim_catalog.status(rec, [rec])
    assert waiting["publication_reason"] == "Waiting for Kiwix to acknowledge the current library catalog"
    with (patch.object(zim_catalog, "active_manifest_matches", return_value=True),
          patch.object(publication, "reader_location", return_value=None)):
        unresolved = zim_catalog.status(rec, [rec])
    assert unresolved["publication_reason"] == "Kiwix reader entry could not be resolved"
    with (patch.object(zim_catalog, "active_manifest_matches", return_value=True),
          patch.object(publication, "reader_location", return_value="/zim/content/ready")):
        ready = zim_catalog.status(rec, [rec])
    assert ready["published"] is True
    assert ready["publication_reason"] == ""
