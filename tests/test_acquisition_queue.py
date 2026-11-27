import threading

import pytest

from theaxiom import acquisition_queue, storage_settings


@pytest.fixture(autouse=True)
def queue_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")


def job(source_id, size):
    return dict(source_id=source_id, title=source_id, operation="pdf",
                configuration={"format": "pdf"}, bytes=size)


def test_reservations_are_atomic_and_released_on_cancel():
    settings = {"storage_root": "/test"}
    first, = acquisition_queue.enqueue([job("first", 80)], 100, settings)
    with pytest.raises(ValueError, match="Insufficient space"):
        acquisition_queue.enqueue([job("second", 10), job("third", 20)], 100, settings)
    with pytest.raises(ValueError, match="already has"):
        acquisition_queue.enqueue([job("first", 10)], 100, settings)
    assert [row["source_id"] for row in acquisition_queue.status()] == ["first"]
    acquisition_queue.cancel(first)
    acquisition_queue.enqueue([job("second", 100)], 100, settings)
    assert [row["state"] for row in acquisition_queue.status()] == ["queued", "cancelled"]


def test_restart_recovers_running_job_before_next_queued_job():
    first, second = acquisition_queue.enqueue([job("first", 30), job("second", 40)], 100, {})
    seen = []
    shutdown = threading.Event()

    def interrupted(row, stopping, shutdown):
        assert row["id"] == first and row["state"] == "queued"
        return "running", ""

    assert not acquisition_queue.run_next(interrupted, shutdown)
    with pytest.raises(ValueError, match="Insufficient"):
        acquisition_queue.enqueue([job("third", 40)], 100, {})

    def finish(row, stopping, shutdown):
        seen.append((row["id"], row["state"]))
        return "completed", ""

    assert acquisition_queue.run_next(finish, shutdown)
    assert acquisition_queue.run_next(finish, shutdown)
    assert not acquisition_queue.run_next(finish, shutdown)
    assert seen == [(first, "running"), (second, "queued")]


def test_running_job_can_see_a_durable_cancel_request():
    job_id, = acquisition_queue.enqueue([job("book", 100)], 100, {})

    def run(row, stopping, shutdown):
        assert not stopping()
        acquisition_queue.cancel(job_id)
        assert stopping()
        raise acquisition_queue.Cancelled()

    assert acquisition_queue.run_next(run, threading.Event())
    assert acquisition_queue.status()[0]["state"] == "cancelled"


def test_automatic_attempt_defers_without_blocking_manual_work(monkeypatch):
    now = 1000
    monkeypatch.setattr(acquisition_queue.time, "time", lambda: now)
    automatic = dict(job("archive", 20), operation="automatic-update", configuration={"digest": "a" * 64})
    auto_id, manual_id = acquisition_queue.enqueue([automatic, job("book", 20)], 50, {})
    shutdown = threading.Event()
    assert not acquisition_queue.run_next(lambda *_: ("deferred", "Window closed"), shutdown)

    def finish_manual(row, *_):
        assert row["id"] == manual_id
        return "completed", ""

    assert acquisition_queue.run_next(finish_manual, shutdown)
    assert not acquisition_queue.run_next(finish_manual, shutdown)
    now += 61

    def fail_automatic(row, *_):
        assert row["id"] == auto_id
        return "failed", "Digest mismatch"

    assert acquisition_queue.run_next(fail_automatic, shutdown)
    with pytest.raises(ValueError, match="already has an automatic attempt"):
        acquisition_queue.enqueue([automatic], 50, {})
    assert acquisition_queue.automatic_attempts()[0]["state"] == "failed"
    acquisition_queue.enqueue([automatic | {"configuration": {"digest": "b" * 64}}], 50, {})
