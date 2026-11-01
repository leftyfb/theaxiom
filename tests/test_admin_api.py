import unittest
from unittest.mock import MagicMock, patch

from theaxiom import admin_api
from theaxiom.freshness_probe import probe_source


class ActionResponseTests(unittest.TestCase):
    def setUp(self):
        admin_api._active_downloads.clear()
        admin_api._jobs.clear()

    @patch.object(admin_api, "probe_source", return_value={"local_current": False})
    @patch.object(admin_api.subprocess, "Popen")
    def test_action_launches_expected_command_once(self, popen, probe):
        response = admin_api.action_response("wikipedia-en", "download")
        self.assertTrue(response["accepted"])
        entry = admin_api.WIKIPEDIA_SOURCE
        popen.assert_called_once_with(
            ["/bin/bash", admin_api.ACQUISITION_SCRIPT,
             entry["source_url"],
             str(admin_api.ACQUISITION_STAGING_DIR / entry["filename"]),
             str(entry["bytes"]), entry["sha256"]],
            stdin=admin_api.subprocess.DEVNULL,
            stdout=admin_api.subprocess.DEVNULL,
            stderr=admin_api.subprocess.DEVNULL,
            start_new_session=True,
        )

    @patch.object(admin_api, "probe_source", return_value={"local_current": False})
    @patch.object(admin_api.subprocess, "Popen")
    def test_redownload_uses_trusted_acquisition_with_overwrite(self, popen, probe):
        response = admin_api.action_response("wikipedia-en", "redownload")
        self.assertTrue(response["accepted"])
        self.assertEqual(response["action"], "redownload")
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "--overwrite")
        self.assertEqual(command[1:-1], [admin_api.ACQUISITION_SCRIPT, *admin_api.build_acquisition_args("wikipedia-en")])

    @patch.object(admin_api, "probe_source", return_value={"local_current": False})
    @patch.object(admin_api.subprocess, "Popen")
    def test_rejects_second_active_download_for_same_source(self, popen, probe):
        popen.return_value.poll.return_value = None
        first = admin_api.action_response("wikipedia-en", "download")
        second = admin_api.action_response("wikipedia-en", "redownload")
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertIn("already active", second["error"])
        popen.assert_called_once()

    @patch.object(admin_api.subprocess, "Popen")
    @patch.object(admin_api, "probe_source", return_value={"local_current": True})
    def test_current_source_skips_download(self, probe, popen):
        response = admin_api.action_response("wikipedia-en", "download")
        self.assertTrue(response["accepted"])
        self.assertTrue(response["skipped"])
        self.assertEqual(response["status"], "current")
        probe.assert_called_once()
        popen.assert_not_called()

    @patch.object(admin_api, "probe_source", return_value={"local_current": False})
    @patch.object(admin_api.subprocess, "Popen")
    def test_stale_source_runs_acquisition(self, popen, probe):
        response = admin_api.action_response("wikipedia-en", "update")
        self.assertTrue(response["accepted"])
        popen.assert_called_once()


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
