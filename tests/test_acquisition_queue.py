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
