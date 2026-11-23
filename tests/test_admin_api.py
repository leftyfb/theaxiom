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


class FreshnessApiContractTests(unittest.TestCase):
    def test_verified_installation_is_independent_of_remote_length(self):
        entry = admin_api.WIKIPEDIA_SOURCE
        job = {"source_id": entry["source_id"], "filename": entry["filename"],
               "expected_bytes": entry["bytes"], "sha256": entry["sha256"],
               "downloaded_bytes": entry["bytes"], "state": "completed",
               "verified": True, "active": False}
        with patch.object(admin_api, "probe_source", return_value={
            "reachable": True, "origin_length_matches_manifest": False,
            "local_current": False, "error": ""}):
            result = admin_api.freshness_response({"stale": False, "acquisitions": [job]})
        self.assertTrue(result["installed_verified"])
        self.assertTrue(result["local_current"])
        self.assertFalse(result["origin_length_matches_manifest"])
        self.assertEqual(result["installed_state"], "verified")

    def test_remote_length_match_does_not_verify_missing_archive(self):
        entry = admin_api.WIKIPEDIA_SOURCE
        job = {"source_id": entry["source_id"], "filename": entry["filename"],
               "expected_bytes": entry["bytes"], "sha256": entry["sha256"],
               "downloaded_bytes": 0, "state": "not_started",
               "verified": False, "active": False}
        with patch.object(admin_api, "probe_source", return_value={
            "reachable": True, "origin_length_matches_manifest": True,
            "local_current": False, "error": ""}):
            result = admin_api.freshness_response({"stale": False, "acquisitions": [job]})
        self.assertFalse(result["installed_verified"])
        self.assertFalse(result["local_current"])
        self.assertTrue(result["origin_length_matches_manifest"])
        self.assertEqual(result["installed_state"], "absent")

    def test_stale_monitor_never_reports_local_current(self):
        with patch.object(admin_api, "probe_source", return_value={
            "reachable": True, "origin_length_matches_manifest": True,
            "local_current": False, "error": ""}):
            result = admin_api.freshness_response({"stale": True, "acquisitions": []})
        self.assertFalse(result["local_current"])
        self.assertEqual(result["installed_state"], "unknown")


    def test_same_size_corrupt_archive_does_not_appear_current(self):
        entry = admin_api.WIKIPEDIA_SOURCE
        job = {"source_id": entry["source_id"], "filename": entry["filename"],
               "expected_bytes": entry["bytes"], "sha256": entry["sha256"],
               "downloaded_bytes": entry["bytes"], "state": "failed",
               "verified": False, "active": False}
        with patch.object(admin_api, "probe_source", return_value={
            "reachable": True, "origin_length_matches_manifest": True,
            "local_current": False, "error": ""}):
            result = admin_api.freshness_response({"stale": False, "acquisitions": [job]})
        self.assertFalse(result["local_current"])
        self.assertEqual(result["installed_state"], "failed_or_corrupt")

    def test_unavailable_storage_is_not_reported_as_absent_or_verified(self):
        entry = admin_api.WIKIPEDIA_SOURCE
        job = {"source_id": entry["source_id"], "filename": entry["filename"],
               "expected_bytes": entry["bytes"], "sha256": entry["sha256"],
               "downloaded_bytes": None, "state": "unavailable",
               "verified": False, "active": False}
        with patch.object(admin_api, "probe_source", return_value={
            "reachable": False, "origin_length_matches_manifest": None,
            "local_current": False, "error": "timeout"}):
            result = admin_api.freshness_response({"stale": False, "acquisitions": [job]})
        self.assertFalse(result["local_current"])
        self.assertEqual(result["installed_state"], "unavailable")
        self.assertEqual(result["error"], "timeout")


class ZimReplacementPublicResponseTests(unittest.TestCase):
    def test_only_ui_fields_are_exposed(self):
        internal = {
            "state": "activated",
            "downloaded_bytes": 123,
            "expected_bytes": 456,
            "error": "review needed",
            "previous_job": {"artifact": {"path": "/private/archive.zim"}},
            "artifact": {"path": "/private/replacement.zim"},
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
