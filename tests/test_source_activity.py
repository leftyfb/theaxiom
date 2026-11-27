from concurrent.futures import ThreadPoolExecutor

from theaxiom import catalog_store, publication, source_activity, storage_settings


def test_history_durable_filter_pagination_and_removed_source(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    revision = store.get()["revision"]
    for operation, fields in [
        ("add", {"title": "History fixture", "source_url": "https://example.org/private-token"}),
        ("edit", {"title": "Renamed fixture", "edition": "2"}),
    ]:
        revision = store.mutate(operation, "history-fixture", fields, revision)["revision"]
    publication.save("history-fixture", True)
    publication.save("history-fixture", True)
    publication.save("history-fixture", False)
    store.mutate("remove", "history-fixture", {}, revision)
    result = source_activity.query("history-fixture", limit=2)
    assert [e["action"] for e in result["events"]] == ["removed", "unpublished"]
    older = source_activity.query("history-fixture", before=result["next"], limit=100)
    assert [e["action"] for e in older["events"]] == ["published", "edited", "added"]
    assert older["events"][-1]["name"] == "History fixture"
    assert result["sources"] == [{"source_id": "history-fixture", "name": "Renamed fixture"}]
    assert source_activity.query(action="edited")["events"][0]["new_edition"] == "2"
    assert b"private-token" not in source_activity.path().read_bytes()


def test_intent_recovery_and_retry_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    file = tmp_path / "state.json"
    file.write_bytes(b"before")
    kwargs = dict(sid="fixture", name="Fixture", action="edited", key="fixture-event")
    source_activity.prepare_write(file, b"after", **kwargs)
    assert source_activity.query()["events"] == []
    file.write_bytes(b"after")
    assert len(source_activity.query()["events"]) == 1
    assert len(source_activity.query()["events"]) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: source_activity.record("fixture", "downloaded", name="Fixture", key="download"), range(8)))
    assert len(source_activity.query()["events"]) == 2
    source_activity.record("fixture", "download_failed", name="Fixture", outcome="failed")
    source_activity.acknowledge("consumer", 999, retain=1)
    assert len(source_activity.query()["events"]) == 1
    source_activity.record("fixture", "downloaded", name="Fixture", key="download")
    assert len(source_activity.query()["events"]) == 1


def test_no_completed_update_for_failed_or_staged_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    source_activity.record("fixture", "download_failed", name="Fixture", outcome="failed")
    source_activity.record("fixture", "download_cancelled", name="Fixture", outcome="cancelled")
    assert not source_activity.query(action="updated")["events"]
    source_activity.record("fixture", "updated", name="Fixture", trigger="scheduled",
                           old_edition="1", new_edition="2", key="activated")
    source_activity.record("fixture", "rolled_back", name="Fixture", key="rollback")
    assert [e["action"] for e in source_activity.query()["events"]] == [
        "rolled_back", "updated", "download_cancelled", "download_failed"]


def test_delete_recovery_requires_original_directory_and_all_files(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    directory = tmp_path / "library"
    directory.mkdir()
    for name in ("one.pdf", "two.pdf"):
        (directory / name).write_bytes(b"fixture")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_activity.prepare_delete(fd, ["one.pdf", "two.pdf"], sid="fixture", name="Fixture")
        (directory / "one.pdf").unlink()
        assert not source_activity.query()["events"]
        (directory / "two.pdf").unlink()
        assert source_activity.query()["events"][0]["action"] == "deleted"
        assert len(source_activity.query()["events"]) == 1
    finally:
        os.close(fd)

def test_update_queue_history_deferral_deduplicates_and_excludes_error_text(tmp_path, monkeypatch):
    from theaxiom import acquisition_queue as queue
    import threading
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    job = dict(source_id="fixture", title="Fixture", operation="automatic-update", bytes=12,
               configuration={"original": {"update_policy": "scheduled"}, "fields": {"edition": "2"},
                              "digest": "a" * 64})
    jid, = queue.enqueue([job], 100, {})
    for _ in range(2):
        with queue.database() as db:
            db.execute("UPDATE automatic_attempts SET ready_at=0")
        queue.run_next(lambda *_: ("deferred", "Waiting for source archive worker or storage maintenance"), threading.Event())
    history = source_activity.query()["events"]
    assert [row["action"] for row in history] == ["update_deferred", "update_started", "update_queued"]
    assert history[0]["reason"] == "Archive worker or storage maintenance busy"
    assert all(row["trigger"] == "scheduled" for row in history)
    with queue.database() as db:
        db.execute("UPDATE automatic_attempts SET ready_at=0")
    queue.run_next(lambda *_: ("failed", "private-secret-fixture"), threading.Event())
    assert source_activity.query()["events"][0]["action"] == "update_failed"
    assert "private-secret-fixture" not in str(source_activity.query())
    assert not source_activity.query(action="updated")["events"]
    assert jid

def test_update_notifications_replay_lease_ack_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    now = [100.0]
    monkeypatch.setattr(source_activity.time, "time", lambda: now[0])
    for action in ("update_discovered", "staged", "rolled_back", "update_failed"):
        source_activity.record("public", action, name="Public")
    for sid, trigger in (("private", "automatic"), ("public", "scheduled"), ("other", "batch"), ("manual", "manual")):
        source_activity.record(sid, "updated", name=sid, new_edition="2", trigger=trigger)
    source_activity.record("public", "updated", name="Public", outcome="failed")
    assert source_activity.has_update_notification({"public"})
    allowed = {"public": "Public"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        notifications = list(pool.map(lambda _: source_activity.claim_update_notification(allowed), range(4)))
    claimed, = [item for item in notifications if item]
    assert claimed["name"] == "Public" and claimed["edition"] == "2"
    assert source_activity.claim_update_notification(allowed) is None
    # A restarted process or closed browser never acknowledges its lease.
    now[0] += source_activity.NOTIFICATION_LEASE + 1
    replay = source_activity.claim_update_notification(allowed)
    assert replay["event_id"] == claimed["event_id"] and replay["claim"] != claimed["claim"]
    assert not source_activity.acknowledge_update_notification(claimed["event_id"], claimed["claim"])
    assert not source_activity.acknowledge_update_notification(replay["event_id"], replay["claim"], {})
    assert source_activity.acknowledge_update_notification(replay["event_id"], replay["claim"], allowed)
    assert source_activity.acknowledge_update_notification(replay["event_id"], replay["claim"], allowed)
    assert not source_activity.has_update_notification(allowed)
    for name in ("private", "other", "manual"):
        item = source_activity.claim_update_notification()
        assert item["name"] == name
        assert source_activity.acknowledge_update_notification(item["event_id"], item["claim"])
    assert source_activity.claim_update_notification() is None
    assert len(source_activity.query(action="updated")["events"]) == 5


def test_unseen_notifications_survive_activity_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    source_activity.record("fixture", "updated", name="Fixture")
    for _ in range(4):
        source_activity.record("fixture", "edited", name="Fixture")
    source_activity.acknowledge("other-consumer", 999, retain=1)
    item = source_activity.claim_update_notification()
    assert item and item["name"] == "Fixture"
    assert source_activity.acknowledge_update_notification(item["event_id"], item["claim"])
