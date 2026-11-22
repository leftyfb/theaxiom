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
