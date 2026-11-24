from theaxiom import publication, storage_settings


def test_publication_intent_is_private_and_persists_across_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    publication.save("example", True)
    assert publication.load() == {"example": True}
    publication.save("example", False)
    assert not publication.load().get("example", False)
    assert publication.state_path().stat().st_mode & 0o777 == 0o600
