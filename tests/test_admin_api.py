import unittest
from unittest.mock import MagicMock, patch

from theaxiom import admin_api

from theaxiom.freshness_probe import probe_source



class FreshnessProbeTests(unittest.TestCase):
    def probe_with_length(self, content_length):
        response = MagicMock(status=200)
        response.headers = {
            "Content-Length": str(content_length),
            "Last-Modified": "Tue, 15 Sep 2026 12:00:00 GMT",
        }
        opener = MagicMock()
        opener.return_value.__enter__.return_value = response
        result = probe_source("https://example.invalid/archive.zim", "trusted-sha256", 754723, opener=opener)
        opener.assert_called_once()
        return result

    def test_current_probe_result_is_read_correctly(self):
        result = self.probe_with_length(754723)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertTrue(result["origin_length_matches_manifest"])
        self.assertEqual(result["content_length"], "754723")
        self.assertEqual(result["last_modified"], "Tue, 15 Sep 2026 12:00:00 GMT")

    def test_stale_probe_result_is_read_correctly(self):
        result = self.probe_with_length(999999)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertEqual(result["content_length"], "999999")


class ZimReplacementPublicResponseTests(unittest.TestCase):
    def test_only_ui_fields_are_exposed(self):
        internal = {
            "state": "activated",
            "downloaded_bytes": 123,
            "expected_bytes": 456,
            "error": "review needed",
            "previous_job": {"artifact": {"path": "/private/archive.zim"}},
            "artifact": {"path": "/private/replacement.zim", "sha256": "a" * 64},
            "configuration": {"source_url": "https://private.example/archive.zim"},
            "base_artifact": {"sha256": "secret"},
            "etag": '"internal"',
            "last_modified": "yesterday",
        }
        self.assertEqual(admin_api._public_zim_replacement(internal), {
            "state": "activated",
            "downloaded_bytes": 123,
            "expected_bytes": 456,
            "error": "review needed",
            "sha256": "a" * 64,
            "review_token": admin_api.zim_jobs.candidate_review_token(internal),
        })
        self.assertIn("previous_job", internal)

    def test_absent_candidate_and_minimal_state(self):
        self.assertIsNone(admin_api._public_zim_replacement(None))
        self.assertEqual(admin_api._public_zim_replacement({"state": "prepared"}), {
            "state": "prepared",
        })


class ZimReplacementManifestRefreshContractTests(unittest.TestCase):
    def test_activation_and_rollback_schedule_reader_reconciliation(self):
        import inspect
        handler = inspect.getsource(admin_api.AdminAPIHandler._start_zim)
        self.assertIn('{"zim-replacement-activate", "zim-replacement-rollback"}', handler)
        self.assertIn('zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])', handler)
        self.assertLess(handler.index('zim_refresh.schedule('), handler.index('self._send_json(202, result)'))


def test_only_recent_active_native_checksum_samples_are_public():
    for project in (admin_api._public_acquisition, admin_api._public_zim_replacement):
        sample = {"state": "verifying", "checksum_bytes": 100, "checksum_pass": 2,
                  "checksum_passes": 2, "checksum_sampled_at": 99}
        with patch.object(admin_api.time, "time", return_value=100):
            visible = project(sample)
            assert visible["checksum_bytes"] == 100
            assert visible["checksum_pass"] == 2
            assert "checksum_sampled_at" not in visible
            assert "checksum_bytes" not in project(sample | {"state": "paused"})
            assert "checksum_bytes" not in project(sample | {"checksum_sampled_at": 80})


def test_update_summary_uses_verified_installed_sources_and_cached_evidence():
    def source(state=None, *, verified=True, method="kiwix", periodic=False, checked=False):
        result = {"state": state} if state else None
        if checked:
            result["checked_at"] = 100
        return {"discovery": {"method": method, "periodic": periodic},
                "installed_verified": verified, "update_check": result}

    assert not admin_api._source_update_summary([
        source("update_available", verified=False), source("update_available", method="manual"),
        source("unable_to_determine"), source("no_newer_edition_listed", periodic=True),
    ])
    assert admin_api._source_update_summary([source("update_available")]) == "1 source has an update available"
    assert admin_api._source_update_summary([
        source("representation_changed", method="head"), source(periodic=True),
        source("unable_to_determine", checked=True),
    ]) == "3 sources need update review"


def test_acquisition_history_exposes_only_finished_outcomes_and_timestamps():
    first = {"state": "completed", "finished_at": 1700000000, "etag": "private-validator"}
    latest = {"state": "failed", "finished_at": 1700000100, "operation": "retry",
              "source_url": "https://private.example.org/path", "detail": "raw error detail"}
    running = {"started_at": 1700000200}
    job = {"state": "downloading", "attempt_history": [first, latest, running]}
    exposed = admin_api._public_acquisition(job)
    assert exposed["last_successful_acquisition"] == first["finished_at"]
    assert exposed["attempt_history"] == [
        {"state": "completed", "finished_at": first["finished_at"]},
        {"state": "failed", "finished_at": latest["finished_at"]},
    ]
    assert exposed["retry_count"] == 1
    assert job["attempt_history"] == [first, latest, running]


def test_initial_and_replacement_use_same_fresh_transfer_projection():
    sample = {"state": "downloading", "active": True, "downloaded_bytes": 200,
              "expected_bytes": 800, "sampled_at": 98, "speed_bytes_per_second": 50,
              "eta_seconds": 999}
    with patch.object(admin_api.time, "time", return_value=100):
        for project in (admin_api._public_acquisition, admin_api._public_zim_replacement):
            visible = project(sample)
            assert visible["speed_bytes_per_second"] == 50
            assert visible["eta_seconds"] == 12
            for fields in ({"sampled_at": 89}, {"sampled_at": 101}, {"state": "retrying"},
                           {"state": "verifying"}, {"state": "failed"}, {"state": "completed"},
                           {"state": "interrupted"}, {"speed_bytes_per_second": 0}):
                # A completed replacement uses the verified state; this fixture
                # tests rate suppression without an artifact or review token.
                result = project(sample | fields)
                assert "speed_bytes_per_second" not in result
                assert "eta_seconds" not in result
            assert "eta_seconds" not in project(sample | {"expected_bytes": None})
            assert "eta_seconds" not in project(sample | {"downloaded_bytes": 800})
    assert sample["eta_seconds"] == 999
