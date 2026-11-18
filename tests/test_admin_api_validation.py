#!/usr/bin/env python3
"""Offline HTTP rejection tests for the admin API.

All requests use an isolated localhost server. Process creation is blocked
to ensure these tests never start a real acquisition.
"""
import json
import subprocess
import threading
import urllib.error
import urllib.request
import unittest
from unittest import mock

from theaxiom.admin_api import AdminAPIHandler, ThreadingHTTPServer


class AdminAPIValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AdminAPIHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post_raw(self, data, content_type="application/json", path="/api/action"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": content_type},
            method="POST",
        )

        # Mock subprocess creation during every HTTP request, not merely
        # around expected-valid actions.
        with mock.patch.object(
            subprocess, "Popen",
            side_effect=AssertionError("Acquisition subprocess forbidden"),
        ), mock.patch.object(
            subprocess, "run",
            side_effect=AssertionError("Acquisition subprocess forbidden"),
        ):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    status = response.status
                    body = response.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read()

        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            parsed = body

        return status, parsed

    def test_storage_settings_get_and_save_persist(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from theaxiom import admin_api

        with TemporaryDirectory() as directory, mock.patch.object(
            admin_api.storage_settings, "SETTINGS_PATH", Path(directory) / "storage.json"
        ):
            defaults = admin_api.storage_settings.DEFAULT_SETTINGS
            updated = dict(defaults, storage_source="/dev/test", minimum_free_bytes=1234)
            status, saved = self.post_raw(
                json.dumps(updated).encode(), path="/api/storage-settings"
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved, updated)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/storage-settings", timeout=5
            ) as response:
                status = response.status
                loaded = json.loads(response.read())
            self.assertEqual(status, 200)
            self.assertEqual(loaded, updated)

    def test_invalid_storage_settings_save_does_not_replace_existing(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from theaxiom import admin_api

        with TemporaryDirectory() as directory, mock.patch.object(
            admin_api.storage_settings, "SETTINGS_PATH", Path(directory) / "storage.json"
        ):
            valid = admin_api.storage_settings.DEFAULT_SETTINGS.copy()
            admin_api.storage_settings.save_settings(valid)
            invalid = dict(valid, storage_mount="/../escape")
            status, body = self.post_raw(
                json.dumps(invalid).encode(), path="/api/storage-settings"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body, {"error": "invalid storage settings"})
            self.assertEqual(admin_api.storage_settings.load_settings(), valid)

    def post_action(self, source_id, action):
        return self.post_raw(
            json.dumps({
                "source_id": source_id,
                "action": action,
            }).encode("utf-8")
        )

    def test_unknown_action(self):
        status, _ = self.post_action("wikipedia-en", "delete")
        self.assertEqual(status, 400)

    def test_admin_page_is_served_and_connects_to_api(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/admin", timeout=5
        ) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("Manage offline sources", body)
        self.assertIn('data-source-id="wikipedia-en"', body)
        self.assertIn("fetch('/api/action'", body)

    def test_unknown_source_id(self):
        status, _ = self.post_action("nonexistent-source", "download")
        self.assertEqual(status, 400)

    def test_empty_action(self):
        status, _ = self.post_action("wikipedia-en", "")
        self.assertEqual(status, 400)

    def test_empty_source_id(self):
        status, _ = self.post_action("", "download")
        self.assertEqual(status, 400)

    def test_malformed_json(self):
        status, _ = self.post_raw(b'{"source_id":')
        self.assertEqual(status, 400)

    def test_missing_action(self):
        status, _ = self.post_raw(
            json.dumps({"source_id": "wikipedia-en"}).encode()
        )
        self.assertEqual(status, 400)

    def test_missing_source_id(self):
        status, _ = self.post_raw(
            json.dumps({"action": "download"}).encode()
        )
        self.assertEqual(status, 400)

    def test_json_array_instead_of_object(self):
        status, _ = self.post_raw(b'[]')
        self.assertEqual(status, 400)

    def test_json_null_instead_of_object(self):
        status, _ = self.post_raw(b'null')
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
