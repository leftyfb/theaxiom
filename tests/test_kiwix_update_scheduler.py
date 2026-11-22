"""Offline tests for metadata-only scheduled update sweeps."""
import pytest

from theaxiom import kiwix_update_scheduler as scheduler


def test_only_zim_source_records_are_checked_in_stable_order():
    records = [
        {"source_id": "zeta", "configuration": {"format": "zim"}},
        {"source_id": "alpha", "configuration": {"format": "zim"}},
        {"source_id": "text", "configuration": {"format": "pdf"}},
        {"source_id": "wikipedia-en", "configuration": {"format": "zim"}},
        {"source_id": "zeta", "configuration": {"format": "zim"}},
    ]
    calls = []
    def checker(sid):
        calls.append(sid)
        return {"state": "update_available"}
    results = scheduler.check_records(records, checker=checker)
    assert calls == ["alpha", "zeta"]
    assert results == [{"source_id": "alpha", "state": "update_available"},
                       {"source_id": "zeta", "state": "update_available"}]


def test_bounded_daily_sweep_and_fail_closed_result():
    records = [{"source_id": f"source-{i:02}", "configuration": {"format": "zim"}}
               for i in range(25)]
    def checker(sid):
        if sid.endswith("00"):
            raise ValueError("offline")
        return {"state": "not_authoritative"}
    result = scheduler.check_records(records, max_checks=3, checker=checker)
    assert [row["source_id"] for row in result] == ["source-00", "source-01", "source-02"]
    assert all(row["state"] == "unable_to_determine" for row in result)


@pytest.mark.parametrize("limit", [0, -1, 21, True, None, "2"])
def test_invalid_maximum_is_rejected(limit):
    with pytest.raises(ValueError):
        scheduler.check_records([], max_checks=limit)


def test_no_sources_never_contact_network():
    result = scheduler.check_records([], checker=lambda sid: pytest.fail("No checks allowed"))
    assert result == []


def test_missing_catalog_does_not_initialize_or_write(tmp_path, monkeypatch):
    from theaxiom import storage_settings
    monkeypatch.delenv("THEAXIOM_SOURCE_CATALOG", raising=False)
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    assert scheduler.main([]) == 2
    assert list(tmp_path.iterdir()) == []


def test_rotating_bound_prevents_starving_later_sources():
    records = [{"source_id": f"source-{i:02}", "configuration": {"format": "zim"}}
               for i in range(12)]
    first = scheduler.check_records(records, max_checks=5, start_index=0,
                                    checker=lambda sid: {"state": "unable_to_determine"})
    second = scheduler.check_records(records, max_checks=5, start_index=5,
                                     checker=lambda sid: {"state": "unable_to_determine"})
    third = scheduler.check_records(records, max_checks=5, start_index=10,
                                    checker=lambda sid: {"state": "unable_to_determine"})
    seen = {row["source_id"] for row in first + second + third}
    assert len(seen) == 12


@pytest.mark.parametrize("offset", [-1, True, None, "2"])
def test_invalid_rotation_index_is_rejected(offset):
    with pytest.raises(ValueError):
        scheduler.check_records([], start_index=offset)
