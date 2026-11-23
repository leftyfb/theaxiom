from unittest.mock import patch
from theaxiom import publication, storage_settings

def snapshot():
    return {"stale": False, "acquisitions": [{"source_id": "wikipedia-en",
        "state": "completed", "verified": True, "active": False}]}

def test_publish_unpublish_and_restart(tmp_path):
    with patch.object(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json"), patch.object(publication, "reachable", return_value=True):
        assert not publication.allowed(snapshot(), "wikipedia-en")
        assert publication.status(snapshot(), "wikipedia-en")["serving"]
        publication.save("wikipedia-en", True)
        assert publication.allowed(snapshot(), "wikipedia-en")
        assert publication.status(snapshot(), "wikipedia-en")["published"]
        assert not publication.allowed(dict(snapshot(), stale=True), "wikipedia-en")
        publication.save("wikipedia-en", False)
        assert not publication.allowed(snapshot(), "wikipedia-en")
        assert not publication.status(snapshot(), "wikipedia-en")["published"]
        assert publication.state_path().stat().st_mode & 0o777 == 0o600

def test_reader_failure_and_corrupt_state(tmp_path):
    with patch.object(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json"), patch.object(publication, "reachable", return_value=False):
        publication.save("wikipedia-en", True)
        assert not publication.status(snapshot(), "wikipedia-en")["published"]
        publication.state_path().write_text("invalid")
        assert not publication.allowed(snapshot(), "wikipedia-en")


def test_wikipedia_reachability_uses_shared_reader_and_exact_book():
    from theaxiom.source_catalog import WIKIPEDIA_SOURCE
    from pathlib import Path
    expected = Path(WIKIPEDIA_SOURCE["filename"]).stem
    with patch.object(publication, "reader_location", return_value="/zim/viewer#" + expected + "/") as locate:
        assert publication.reachable(publication.ROUTES["wikipedia-en"])
        locate.assert_called_once_with({"path": "/zim/", "port": 8081}, expected)
    with patch.object(publication, "reader_location", return_value=None):
        assert not publication.reachable(publication.ROUTES["wikipedia-en"])


def test_wikipedia_status_available_with_shared_reader_and_readygov(tmp_path):
    with (patch.object(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json"),
          patch.object(publication, "reader_location", return_value="/zim/viewer#wikipedia_en_all_maxi_2026-08/")):
        publication.save("wikipedia-en", True)
        assert publication.status(snapshot(), "wikipedia-en")["published"]
        with patch.object(publication, "reader_location", return_value=None):
            assert not publication.status(snapshot(), "wikipedia-en")["published"]


def test_legacy_pinned_zim_ids_are_validated_from_seed_data_not_source_name():
    from pathlib import Path
    from theaxiom import source_catalog
    old = source_catalog.WIKIPEDIA_SOURCE
    synthetic = dict(old, source_id="another-zim",
                     filename="publisher_archive_en_2026-11.zim",
                     sha256="a" * 64, bytes=8192)
    with patch.object(source_catalog, "builtin_sources", return_value=[old, synthetic]):
        assert publication.valid_book_id(Path(old["filename"]).stem)
        assert publication.valid_book_id("publisher_archive_en_2026-11")
        assert not publication.valid_book_id("untrusted_archive_en_2026-11")
        assert not publication.valid_book_id("publisher_archive_en_2026-12")
        assert not publication.valid_book_id("../publisher_archive_en_2026-11")
    for invalid in (dict(synthetic, sha256=""), dict(synthetic, bytes=0),
                    dict(synthetic, filename="../publisher_archive_en_2026-11.zim")):
        with patch.object(source_catalog, "builtin_sources", return_value=[invalid]):
            assert not publication.valid_book_id("publisher_archive_en_2026-11")


def test_legacy_reader_reachability_depends_on_declared_book_not_source_id():
    from theaxiom import source_catalog
    pinned = dict(source_catalog.WIKIPEDIA_SOURCE, source_id="other-book",
                  filename="second_archived_zim_2026-11.zim", sha256="b" * 64,
                  bytes=4096)
    route = {"path": "/legacy-other/", "reader_path": "/zim/", "port": 8081,
             "book_id": "second_archived_zim_2026-11"}
    with patch.object(source_catalog, "builtin_sources", return_value=[pinned]):
        with patch.object(publication, "reader_location", return_value="/zim/viewer#second_archived_zim_2026-11/") as locate:
            assert publication.reachable(route)
            locate.assert_called_once_with({"path": "/zim/", "port": 8081},
                                           "second_archived_zim_2026-11")
        with patch.object(publication, "reader_location", return_value=None):
            assert not publication.reachable(route)
