"""Source and digest-bound capture-time document validator evidence."""
import os
import pytest

from theaxiom import document_origin_evidence as evidence
from theaxiom import storage_settings

URL = "https://publisher.org/guide.pdf"
SHA = "a" * 64
ETAG = '"release-1"'


@pytest.fixture
def setup_root(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    return tmp_path / "document-origin-validators"


def test_saved_validator_requires_matching_installed_digest_and_url(setup_root):
    evidence.save("guide", "pdf", URL, SHA, ETAG)
    assert evidence.load("guide", "pdf", URL, SHA) == ETAG
    assert evidence.load("guide", "pdf", URL, "b" * 64) is None
    assert evidence.load("guide", "epub", URL, SHA) is None
    assert evidence.load("guide", "pdf", "https://publisher.org/new.pdf", SHA) is None
    assert evidence.load("other", "pdf", URL, SHA) is None
    assert (setup_root / "guide.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("etag", ["", "W/\"weak\"", "unquoted", "\"line\nfeed\""])
def test_weak_or_missing_etag_not_persisted(setup_root, etag):
    with pytest.raises(ValueError):
        evidence.save("guide", "pdf", URL, SHA, etag)
    assert not setup_root.exists()


def test_corrupt_or_world_readable_record_fails_closed(setup_root):
    evidence.save("guide", "pdf", URL, SHA, ETAG)
    target = setup_root / "guide.json"
    target.write_text("{")
    assert evidence.load("guide", "pdf", URL, SHA) is None
    evidence.save("guide", "pdf", URL, SHA, ETAG)
    os.chmod(target, 0o644)
    assert evidence.load("guide", "pdf", URL, SHA) is None


def test_symlink_read_and_write_cannot_follow_external_path(setup_root, tmp_path):
    setup_root.mkdir()
    secret = tmp_path / "private"
    secret.write_text("unchanged")
    (setup_root / "guide.json").symlink_to(secret)
    assert evidence.load("guide", "pdf", URL, SHA) is None
    with pytest.raises(ValueError, match="Unsafe"):
        evidence.save("guide", "pdf", URL, SHA, ETAG)
    assert secret.read_text() == "unchanged"


@pytest.mark.parametrize("name", ["", "../guide", "Guide", "a" * 80])
def test_invalid_source_id_never_creates_records(setup_root, name):
    with pytest.raises(ValueError):
        evidence.save(name, "pdf", URL, SHA, ETAG)
    assert not setup_root.exists()
