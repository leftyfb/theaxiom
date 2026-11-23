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
    assert calls == ["alpha", "wikipedia-en", "zeta"]
    assert results == [{"source_id": "alpha", "state": "update_available"},
                       {"source_id": "wikipedia-en", "state": "update_available"},
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


def test_url_based_documents_are_checked_but_local_uploads_are_not(monkeypatch):
    from theaxiom import document_update_discovery, kiwix_update_discovery
    calls = []
    monkeypatch.setattr(document_update_discovery, "check_installed_document",
                        lambda record: (calls.append(("document", record["source_id"])) or
                                        {"state": "representation_changed"}))
    monkeypatch.setattr(kiwix_update_discovery, "refresh_checked_source",
                        lambda sid: (calls.append(("zim", sid)) or
                                     {"state": "unable_to_determine"}))
    records = [
        {"source_id": "upload-only", "configuration": {"format": "pdf", "source_url": ""}},
        {"source_id": "guide-pdf", "configuration": {
            "format": "pdf", "source_url": "https://publisher.org/guide.pdf"}},
        {"source_id": "book-epub", "configuration": {
            "format": "epub", "source_url": "https://publisher.org/book.epub"}},
        {"source_id": "zim", "configuration": {"format": "zim", "source_url": "https://download.kiwix.org/book.zim"}},
    ]
    result = scheduler.check_records(records, max_checks=10)
    assert set(calls) == {("document", "book-epub"), ("document", "guide-pdf"), ("zim", "zim")}
    states = {item["source_id"]: item["state"] for item in result}
    assert states["book-epub"] == "representation_changed"
    assert states["guide-pdf"] == "representation_changed"
    assert states["zim"] == "unable_to_determine"
    assert "upload-only" not in states


def test_scheduled_pinned_zim_uses_verified_generic_seed_gate(monkeypatch):
    from theaxiom import acquisition_monitor, admin_api, kiwix_update_discovery, publication
    sid = next(iter(publication.ROUTES))
    record = {"source_id": sid, "configuration": {"format": "zim"}}
    calls = []
    monkeypatch.setattr(admin_api, "select_lifecycle",
                        lambda source_id, config: {"artifact": {"sha256": "a" * 64, "filename": "example.zim", "bytes": 100}})
    monkeypatch.setitem(admin_api.TRUSTED_SOURCES, sid, {"source_id": sid})
    monkeypatch.setattr(acquisition_monitor, "collect",
                        lambda entry: {"state": "completed", "verified": True, "active": False,
                                       "filename": "example.zim", "expected_bytes": 100,
                                       "sha256": "a" * 64})
    def check(entry, artifact, *, verified):
        calls.append((entry, artifact, verified))
        return {"state": "no_newer_edition_listed"}
    monkeypatch.setattr(kiwix_update_discovery, "refresh_pinned_seed", check)
    result = scheduler.check_records([record])
    assert result == [{"source_id": sid, "state": "no_newer_edition_listed"}]
    assert calls == [(record, {"sha256": "a" * 64, "filename": "example.zim", "bytes": 100}, True)]


def test_scheduled_pinned_zim_does_not_assume_installed_verification(monkeypatch):
    from theaxiom import admin_api, kiwix_update_discovery, publication, zim_catalog
    sid = next(iter(publication.ROUTES))
    record = {"source_id": sid, "configuration": {"format": "zim"}}
    monkeypatch.setattr(admin_api, "select_lifecycle",
                        lambda source_id, config: {"artifact": None})
    monkeypatch.setitem(admin_api.TRUSTED_SOURCES, sid, {"source_id": sid})
    monkeypatch.setattr(acquisition_monitor, "collect",
                        lambda entry: {"state": "unknown", "verified": False, "active": False})
    def check(entry, artifact, *, verified):
        assert not verified and artifact is None
        return {"state": "unable_to_determine"}
    monkeypatch.setattr(kiwix_update_discovery, "refresh_pinned_seed", check)
    assert scheduler.check_records([record]) == [
        {"source_id": sid, "state": "unable_to_determine"}]


def test_update_check_systemd_units_are_metadata_only_and_daily():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "deploy"
    service = (root / "theaxiom-update-check.service").read_text()
    timer = (root / "theaxiom-update-check.timer").read_text()
    assert "User=theaxiom" in service
    assert "theaxiom.kiwix_update_scheduler" in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=theaxiom-update-check.service" in timer
    assert "WantedBy=timers.target" in timer
