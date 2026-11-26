"""Offline regression tests for metadata-only publisher sweeps."""
from datetime import datetime
import fcntl
from pathlib import Path
import subprocess
import threading
from zoneinfo import ZoneInfo

import pytest

from theaxiom import kiwix_update_scheduler as scheduler, storage_settings, update_schedule


def record(sid, **config):
    return {"source_id": sid, "configuration": {"format": "zim", **config}}


def test_sweep_attempts_every_enabled_source_with_bounded_parallelism(monkeypatch):
    records = [record(f"source-{i:02}") for i in range(43)]
    records += [
        record("opt-out", update_periodic="no"),
        record("manual", update_method="manual"),
        record("uninstalled"),
    ]
    calls = set()
    gate = threading.Barrier(scheduler.WORKERS)

    def check(row, *, manual):
        sid = row["source_id"]
        calls.add(sid)
        assert not manual
        if sid in {f"source-{i:02}" for i in range(scheduler.WORKERS)}:
            gate.wait(5)
        if sid == "source-00":
            raise subprocess.TimeoutExpired("metadata worker", 60)
        if sid == "uninstalled":
            return None
        return {"state": "up_to_date"}

    monkeypatch.setattr(scheduler, "_check_with_deadline", check)
    outcomes = scheduler.check_records(records)
    assert calls == {"uninstalled", *(f"source-{i:02}" for i in range(43))}
    assert len(outcomes) == 43
    assert outcomes[0] == {"source_id": "source-00", "state": "unable_to_determine",
                           "evidence": "Publisher metadata check exceeded the per-source deadline."}
    assert all(row["state"] == "up_to_date" for row in outcomes[1:])


def test_deadline_stops_the_worker_process(monkeypatch):
    monkeypatch.setattr(scheduler, "SOURCE_TIMEOUT", 0.05)
    real_run = subprocess.run

    def stall(command, **kwargs):
        assert command[-1] == "--worker"
        return real_run([command[0], "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", stall)
    with pytest.raises(subprocess.TimeoutExpired):
        scheduler._check_with_deadline(record("stall"), manual=False)


def test_only_metadata_adapters_are_called_and_manual_includes_periodic_opt_out(monkeypatch):
    calls = {}
    def document(row, *, installed_only):
        assert installed_only
        calls[row["source_id"]] = "head"
        return {"state": "representation_changed"}

    def zim(sid, *, force, installed_only):
        assert force and installed_only
        calls[sid] = "kiwix"
        return {"state": "update_available"}

    monkeypatch.setattr(scheduler.document_update_discovery, "check_installed_document", document)
    monkeypatch.setattr(scheduler.kiwix_update_discovery, "refresh_checked_source", zim)
    monkeypatch.setattr(scheduler, "_check_with_deadline", scheduler._check_source)
    result = scheduler.check_records([
        record("offline", update_periodic="no"),
        record("manual", update_method="manual"),
        record("upload-only", format="pdf"),
        record("guide", format="pdf", source_url="https://publisher.org/guide.pdf"),
        record("book", format="epub", source_url="https://publisher.org/book.epub"),
    ], manual=True)
    assert calls == {"offline": "kiwix", "guide": "head", "book": "head"}
    assert {item["state"] for item in result} == {"update_available", "representation_changed"}


def test_missing_catalog_does_not_initialize_or_write(tmp_path, monkeypatch):
    monkeypatch.delenv("THEAXIOM_SOURCE_CATALOG", raising=False)
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    assert scheduler.main([]) == 2
    assert list(tmp_path.iterdir()) == []


def test_one_lock_prevents_manual_and_scheduled_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    lock_path = update_schedule.state_path().with_suffix(".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for kwargs in ({}, {"custom": True}, {"manual": True}):
            # The missing catalog would fail if any competing invocation entered.
            assert scheduler._run(tmp_path / "missing", **kwargs) is None


def test_default_timer_and_service_are_preserved():
    root = Path(__file__).resolve().parents[1] / "deploy"
    service = (root / "theaxiom-update-check.service").read_text()
    timer = (root / "theaxiom-update-check.timer").read_text()
    assert "User=theaxiom" in service
    assert "theaxiom.kiwix_update_scheduler" in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "RandomizedDelaySec=1h" in timer
    assert "Persistent=true" in timer


def test_schedule_persists_across_runs_and_tracks_local_time(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 6, 20, 0, tzinfo=zone)
    update_schedule.save(update_schedule.DEFAULT, now)
    assert update_schedule.current() is None
    preferences = {"interval_days": 2, "start_time": "04:30"}
    update_schedule.save(preferences, now)
    before, upcoming = update_schedule.slot(update_schedule.current(), now)
    assert before is None
    assert upcoming.isoformat() == "2026-03-07T04:30:00-05:00"
    state = update_schedule.current()
    due, upcoming = update_schedule.slot(state, datetime(2026, 3, 9, 4, 40, tzinfo=zone))
    assert due.isoformat() == "2026-03-09T04:30:00-04:00"
    assert upcoming.isoformat() == "2026-03-11T04:30:00-04:00"
    update_schedule.complete(state, due)
    assert update_schedule.current()["settings"] == preferences
    assert update_schedule.slot(update_schedule.current(), due)[0] is None


def test_schedule_change_during_sweep_does_not_mark_new_schedule_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    now = datetime(2026, 11, 26, 2, 0, tzinfo=ZoneInfo("UTC"))
    update_schedule.save({"interval_days": 3, "start_time": "02:30"}, now)
    current = update_schedule.current()
    due, _ = update_schedule.slot(current, now.replace(hour=3))
    update_schedule.save({"interval_days": 2, "start_time": "05:00"}, now)
    update_schedule.complete(current, due)
    assert update_schedule.current()["completed"] is None
