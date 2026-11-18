import unittest
from unittest.mock import MagicMock, patch

from theaxiom import admin_api
from theaxiom.freshness_probe import probe_source


class ActionResponseTests(unittest.TestCase):
    @patch.object(admin_api, "probe_source")
    @patch.object(admin_api.subprocess, "Popen")
    def test_all_acquisitions_fail_closed_before_probe_or_launch(self, popen, probe):
        for action in ("download", "redownload", "update"):
            response = admin_api.action_response("wikipedia-en", action)
            self.assertFalse(response["accepted"])
            self.assertIn("acquisition disabled", response["error"])
        popen.assert_not_called()
        probe.assert_not_called()

    def test_unknown_source_rejected(self):
        self.assertEqual(admin_api.action_response("invalid", "download"),
                         {"accepted": False, "error": "unknown source_id"})


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
        self.assertTrue(result["local_current"])
        self.assertEqual(result["content_length"], "754723")
        self.assertEqual(result["last_modified"], "Tue, 15 Sep 2026 12:00:00 GMT")

    def test_stale_probe_result_is_read_correctly(self):
        result = self.probe_with_length(999999)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertEqual(result["content_length"], "999999")
