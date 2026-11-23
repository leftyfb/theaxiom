import json

import pytest

from theaxiom import update_diagnostics as diagnostics


@pytest.mark.parametrize("phase", sorted(diagnostics._PHASES))
def test_attempt_records_safe_fields_only(monkeypatch, phase):
    entries = []
    monkeypatch.setattr(diagnostics.LOGGER, "log", lambda level, fmt, item: entries.append(json.loads(item)))
    with diagnostics.attempt("offline-guide", {"endpoint": "https://publisher.org/path?token=secret"},
                             trigger="scheduled") as outcome:
        diagnostics.mark(phase, http_status=503, cache="miss")
        outcome.update(state="unable_to_determine", evidence="XML from 192.168.1.1",
                       candidate_url="https://secret-user@publisher.org/file")
    assert len(entries) == 1
    event = entries[0]
    assert event["phase"] == phase
    assert event["reason_code"] == phase + "_failed"
    assert event["http_status"] == 503
    assert event["publisher_host"] == "publisher.org"
    assert event["cache"] == "miss"
    assert len(event["request_id"]) == 32
    assert event["duration_ms"] >= 0
    assert event["checked_at"] > 0
    for forbidden in ("secret", "XML", "192.168.1.1", "/path", "/file"):
        assert forbidden not in repr(event)


def test_cache_age_and_context_reset(monkeypatch):
    entries = []
    monkeypatch.setattr(diagnostics.LOGGER, "log", lambda level, fmt, item: entries.append(json.loads(item)))
    monkeypatch.setattr(diagnostics.time, "time", lambda: 1000)
    with diagnostics.attempt("first", {"endpoint": ""}, trigger="manual") as outcome:
        diagnostics.mark("cache_read", cache="hit")
        outcome.update(state="no_newer_edition_listed", checked_at=950)
    diagnostics.mark("busy")
    with diagnostics.attempt("second", {"endpoint": ""}, trigger="manual") as outcome:
        outcome.update(state="unable_to_determine")
    assert entries[0]["cache_age_seconds"] == 50
    assert entries[1]["phase"] == "installed_identity"
    assert "cache_age_seconds" not in entries[1]
    assert entries[0]["request_id"] != entries[1]["request_id"]
