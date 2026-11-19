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
            updated = dict(defaults, storage_root="/tmp/library", minimum_free_bytes=1234)
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

    def post_source(self, payload, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/sources",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_source_catalog_api_crud_persists_and_preserves_content(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from theaxiom import admin_api, catalog_store

        with TemporaryDirectory() as directory, mock.patch.object(
            admin_api, "SOURCE_CATALOG", catalog_store.CatalogStore(Path(directory) / "sources.json")
        ):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/sources") as response:
                initial = json.loads(response.read())
            status, added = self.post_source({
                "operation": "add", "source_id": "field-guide", "revision": initial["revision"],
                "fields": {"title": "Field Guide", "description": "Candidate", "source_url": "https://example.org/guide.pdf", "publisher": "Example", "edition": "2026 edition", "language": "en", "expected_size_bytes": "1536", "sha256": "b" * 64, "provenance": "Publisher catalog", "license": "CC BY 4.0"},
            })
            self.assertEqual(status, 200)
            self.assertTrue(next(s for s in added["sources"] if s["source_id"] == "field-guide")["candidate_only"])
            status, stale = self.post_source({"operation": "edit", "source_id": "field-guide", "revision": initial["revision"], "fields": {"title": "Stale"}})
            self.assertEqual(status, 409)
            self.assertEqual(stale, {"error": "catalog revision changed"})
            status, edited = self.post_source({"operation": "edit", "source_id": "field-guide", "revision": added["revision"], "fields": {"title": "Updated Guide"}})
            self.assertEqual(status, 200)
            self.assertEqual(next(s for s in edited["sources"] if s["source_id"] == "field-guide")["title"], "Updated Guide")
            status, removed = self.post_source({"operation": "remove", "source_id": "field-guide", "revision": edited["revision"], "fields": {}})
            self.assertEqual(status, 200)
            self.assertNotIn("field-guide", [s["source_id"] for s in removed["sources"]])

    def test_source_catalog_api_rejects_cross_origin_and_invalid_fields(self):
        payload = {"operation": "add", "source_id": "candidate", "fields": {"title": "Candidate"}, "revision": 0}
        status, body = self.post_source(payload, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": "cross-origin request rejected"})
        status, body = self.post_source({**payload, "source_id": "../bad"})
        self.assertEqual(status, 400)
        self.assertIn("invalid source ID", body["error"])

    def test_source_catalog_api_rejects_busy_source(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from theaxiom import admin_api, catalog_store

        class Busy:
            def snapshot(self):
                return {"stale": False, "acquisitions": [{"source_id": "field-guide", "state": "downloading", "active": True}]}

        with TemporaryDirectory() as directory, mock.patch.object(
            admin_api, "SOURCE_CATALOG", catalog_store.CatalogStore(Path(directory) / "sources.json")
        ), mock.patch.object(self.server, "acquisition_monitor", Busy(), create=True):
            status, body = self.post_source({"operation": "add", "source_id": "field-guide", "revision": 0, "fields": {"title": "Guide"}})
        self.assertEqual(status, 400)
        self.assertIn("active or unresolved", body["error"])

    def post_action(self, source_id, action):
        return self.post_raw(
            json.dumps({
                "source_id": source_id,
                "action": action,
            }).encode("utf-8")
        )

    def test_valid_action_is_disabled_without_subprocess(self):
        for action in ("download", "redownload", "update"):
            status, body = self.post_action("wikipedia-en", action)
            self.assertEqual(status, 503)
            self.assertIn("acquisition disabled", body["error"])

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
        self.assertIn("fetch('/api/acquisitions'", body)
        self.assertNotIn("fetch('/api/action'", body)
        for action in ("download", "redownload", "update"):
            self.assertIn(f'data-action="{action}-zim" disabled', body)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/acquisitions", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"acquisitions": [], "stale": True})

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
