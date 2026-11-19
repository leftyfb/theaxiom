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
