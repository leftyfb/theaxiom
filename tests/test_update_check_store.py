"""No-network persisted update-check evidence fixtures."""
import json
import os

import pytest

from theaxiom import update_check_store as store

SHA = "a" * 64
NEW = {
    "state": "update_available",
    "evidence": "authenticated publisher catalog comparison",
    "installed_edition": "2026-08",
    "candidate_edition": "2026-11",
    "candidate_url": "https://download.kiwix.org/zim/wikipedia/test_2026-11.zim",
    "candidate_sha256": "b" * 64,
    "candidate_bytes": 123456,
}


@pytest.fixture
def location(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "root_path", lambda: tmp_path / "state")
    return tmp_path / "state"


def test_update_evidence_is_persisted_atomically_and_scoped(location):
    store.save("wikipedia", SHA, NEW, now=1000)
    target = location / "wikipedia.json"
    assert target.exists()
    assert target.stat().st_mode & 0o077 == 0
    result = store.load("wikipedia", SHA, now=1001)
    assert result["state"] == "update_available"
    assert result["candidate_sha256"] == "b" * 64
    assert result["candidate_bytes"] == 123456
    assert result["checked_at"] == 1000
    assert sorted(p.name for p in location.iterdir()) == ["wikipedia.json"]


def test_changed_installed_digest_never_reuses_old_update_evidence(location):
    store.save("wikipedia", SHA, NEW, now=1000)
    assert store.load("wikipedia", "b" * 64, now=1001)["state"] == "unable_to_determine"
    assert store.load("another", SHA, now=1001)["state"] == "unable_to_determine"


def test_expired_or_future_check_cannot_claim_update(location):
    store.save("wikipedia", SHA, NEW, now=1000)
    assert store.load("wikipedia", SHA, now=999)["state"] == "unable_to_determine"
    assert store.load("wikipedia", SHA, now=1000 + store.MAX_AGE + 1)["state"] == "unable_to_determine"


@pytest.mark.parametrize("invalid", [
    {"state": "update_available", "evidence": "missing digest",
     "candidate_url": "https://download.kiwix.org/a.zim"},
    {"state": "update_available", "candidate_sha256": "b" * 64,
     "candidate_url": "http://download.kiwix.org/a.zim"},
    {"state": "random"}, {"state": "up_to_date", "password": "secret"},
    {"state": "up_to_date", "evidence": "line\nfeed"},
])
def test_untrusted_or_invalid_status_cannot_be_saved(location, invalid):
    with pytest.raises(ValueError):
        store.save("wikipedia", SHA, invalid, now=1000)
    assert not (location / "wikipedia.json").exists()


def test_corrupt_or_broadly_accessible_record_fails_closed(location):
    store.save("wikipedia", SHA, NEW, now=1000)
    target = location / "wikipedia.json"
    target.write_text("invalid")
    assert store.load("wikipedia", SHA, now=1001)["state"] == "unable_to_determine"
    target.write_text(json.dumps({
        "source_id": "wikipedia", "installed_sha256": SHA,
        "checked_at": 1000, "result": NEW,
    }))
    os.chmod(target, 0o644)
    assert store.load("wikipedia", SHA, now=1001)["state"] == "unable_to_determine"


def test_read_refuses_symlink_to_external_file(location, tmp_path):
    location.mkdir()
    external = tmp_path / "external"
    external.write_text("do not follow")
    (location / "wikipedia.json").symlink_to(external)
    assert store.load("wikipedia", SHA, now=1000)["state"] == "unable_to_determine"
    with pytest.raises(ValueError, match="Unsafe"):
        store.save("wikipedia", SHA, NEW, now=1000)
    assert external.read_text() == "do not follow"


@pytest.mark.parametrize("sid", ["../other", "a/../b", "", "UPPER", "a" * 100])
def test_invalid_source_ids_never_write(location, sid):
    with pytest.raises(ValueError):
        store.save(sid, SHA, NEW, now=1000)
    assert not location.exists()


def test_ambiguous_local_state_can_be_persisted_without_false_badge(location):
    store.save("wikipedia", SHA, {
        "state": "unable_to_determine", "evidence": "catalog unavailable",
    }, now=1000)
    assert store.load("wikipedia", SHA, now=1001)["state"] == "unable_to_determine"


@pytest.mark.parametrize("quantity", [None, True, -5, 0, 2**63, "123456"])
def test_candidate_bytes_must_be_bounded_integer(location, quantity):
    invalid = dict(NEW, candidate_bytes=quantity)
    with pytest.raises(ValueError):
        store.save("wikipedia", SHA, invalid, now=1000)


def test_no_newer_edition_listed_is_cached_without_claiming_up_to_date(location):
    from theaxiom import update_check_store as store
    result = {
        "state": "no_newer_edition_listed",
        "installed_edition": "2026-10",
        "evidence": "complete filtered catalog listed no later matching edition",
    }
    store.save("ready", SHA, result, now=1000)
    assert store.load("ready", SHA, now=1001)["state"] == "no_newer_edition_listed"
    assert store.load("ready", "b" * 64, now=1001)["state"] == "unable_to_determine"
