"""Generic ZIM manifest requires publication intent and installed verified bytes."""
import xml.etree.ElementTree as ET

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
        "id": "ready", "path": str(disk / "library/zim" / artifact["filename"]),
        "title": "Ready & <Prepared>", "language": "eng"}
    assert b"Ready &amp; &lt;Prepared&gt;" in xml
    assert zim_catalog.render_xml(list(reversed(records)), {"ready": True, "missing": True}) == xml


def test_different_source_configuration_or_invalid_file_not_served(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    wrong = record("ready", config=CONFIG | {"source_url": "https://download.kiwix.org/other.zim"})
    assert zim_catalog.verified_books([wrong], {"ready": True}) == []
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
    assert ET.fromstring(manifest.read_bytes()).find("book").get("id") == "ready"


def test_unpublished_manifest_drops_previous_book_without_removing_archive(disk):
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))
    zim_jobs.save({"source_id": "ready", "state": "completed", "active": False,
                   "verified": True, "configuration": zim_jobs.identity(CONFIG),
                   "artifact": artifact})
    zim_catalog.write_manifest([record("ready")], {"ready": True})
    path = zim_catalog.write_manifest([record("ready")], {"ready": False})
    assert ET.fromstring(path.read_bytes()).find("book") is None
    assert (disk / "library/zim" / artifact["filename"]).exists()
