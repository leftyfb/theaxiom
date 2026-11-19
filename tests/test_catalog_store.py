"""Regression coverage for persistent source catalog management."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from theaxiom import catalog_store


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config" / "sources.json"
        self.store = catalog_store.CatalogStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_custom_source_crud_persists_across_store_restart(self):
        initial = self.store.get()
        saved = self.store.mutate("add", "my-reference", {
            "title": "My Reference", "description": "local candidate",
            "source_url": "https://example.org/reference.pdf", "publisher": "Example",
            "edition": "2026 edition", "language": "en", "expected_size_bytes": "12345",
            "sha256": "a" * 64, "provenance": "Verified from publisher catalog",
            "license": "CC BY 4.0",
        }, initial["revision"])
        self.assertEqual(saved["revision"], 1)
        source = next(x for x in saved["sources"] if x["source_id"] == "my-reference")
        self.assertTrue(source["candidate_only"])
        self.assertFalse(source["builtin"])
        self.assertEqual(source["expected_size_bytes"], "12345")
        self.assertEqual(source["sha256"], "a" * 64)
        self.assertTrue(self.path.exists())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

        restarted = catalog_store.CatalogStore(self.path)
        loaded = restarted.get()
        self.assertIn("my-reference", [x["source_id"] for x in loaded["sources"]])
        edited = restarted.mutate("edit", "my-reference", {"title": "Updated"}, loaded["revision"])
        self.assertEqual(next(x for x in edited["sources"] if x["source_id"] == "my-reference")["title"], "Updated")
        removed = restarted.mutate("remove", "my-reference", {}, edited["revision"])
        self.assertNotIn("my-reference", [x["source_id"] for x in removed["sources"]])

    def test_builtin_metadata_override_is_persistent_and_keeps_stable_id(self):
        initial = self.store.get()
        original = next(s for s in initial["sources"] if s["source_id"] == "wikipedia-en")
        edited = self.store.mutate("edit", "wikipedia-en", {"title": "Reviewed title", "source_url": "https://example.org/candidate.zim", "sha256": "a" * 64, "license": "Candidate license"}, initial["revision"])
        current = next(s for s in edited["sources"] if s["source_id"] == "wikipedia-en")
        self.assertEqual(current["title"], "Reviewed title")
        self.assertEqual(current["source_id"], original["source_id"])
        self.assertEqual(current["source_url"], original["source_url"])
        self.assertEqual(current["candidate_source_url"], "https://example.org/candidate.zim")
        self.assertEqual(current["candidate_sha256"], "a" * 64)
        self.assertEqual(current["candidate_license"], "Candidate license")
        restarted = catalog_store.CatalogStore(self.path).get()
        self.assertEqual(next(s for s in restarted["sources"] if s["source_id"] == "wikipedia-en")["title"], "Reviewed title")

    def test_builtin_remove_is_tombstone_and_preserves_installed_content(self):
        content = Path(self.temp.name) / "content" / "wikipedia-en.zim"
        content.parent.mkdir()
        content.write_bytes(b"installed reference")
        current = self.store.get()
        result = self.store.mutate("remove", "wikipedia-en", {}, current["revision"])
        self.assertNotIn("wikipedia-en", [x["source_id"] for x in result["sources"]])
        self.assertEqual(content.read_bytes(), b"installed reference")
        state = json.loads(self.path.read_text())
        self.assertEqual(state["tombstones"], ["wikipedia-en"])
        self.assertEqual(state["custom"], {})
        self.assertEqual(state["overrides"], {})

    def test_revision_conflict_does_not_overwrite(self):
        revision = self.store.get()["revision"]
        self.store.mutate("add", "one", {"title": "One"}, revision)
        before = self.path.read_bytes()
        with self.assertRaises(catalog_store.RevisionConflict):
            self.store.mutate("add", "two", {"title": "Two"}, revision)
        self.assertEqual(self.path.read_bytes(), before)

    def test_busy_source_rejects_mutation(self):
        with self.assertRaisesRegex(catalog_store.CatalogError, "active or unresolved"):
            self.store.mutate("add", "busy", {"title": "Busy"}, 0, busy=True)
        self.assertFalse(self.path.exists())

    def test_rejects_invalid_ids_fields_urls_and_unknown_operations(self):
        for sid, fields, operation in [
            ("../bad", {"title": "Bad"}, "add"),
            ("good", {"title": "Bad", "unexpected": "x"}, "add"),
            ("good", {"title": "Bad", "source_url": "file:///etc/passwd"}, "add"),
            ("good", {"title": "Bad", "source_url": "https://user:pass@example.org/x"}, "add"),
            ("good", {"title": "Bad", "source_url": "https://example.org/../secret"}, "add"),
            ("good", {"source_url": "https://example.org/guide"}, "add"),
            ("good", {"title": "Bad", "expected_size_bytes": "0"}, "add"),
            ("good", {"title": "Bad", "expected_size_bytes": "1e9"}, "add"),
            ("good", {"title": "Bad", "sha256": "deadbeef"}, "add"),
            ("good", {"title": "Bad"}, "execute"),
        ]:
            with self.subTest(sid=sid, fields=fields, operation=operation):
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.mutate(operation, sid, fields, 0)
        self.assertFalse(self.path.exists())

    def test_corrupt_catalog_fails_closed_without_replacement(self):
        self.path.parent.mkdir(parents=True)
        malformed_states = [
            "not-json",
            json.dumps({"schema_version": True, "revision": 0, "custom": {}, "overrides": {}, "tombstones": []}),
            json.dumps({"schema_version": 1, "revision": 0, "custom": {"custom": {"description": "missing title"}}, "overrides": {}, "tombstones": []}),
            json.dumps({"schema_version": 1, "revision": 0, "custom": {}, "overrides": {}, "tombstones": ["not-a-builtin"]}),
        ]
        for malformed in malformed_states:
            with self.subTest(state=malformed):
                self.path.write_text(malformed)
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.get()
                self.assertEqual(self.path.read_text(), malformed)

    def test_write_failure_preserves_prior_catalog(self):
        first = self.store.mutate("add", "one", {"title": "One"}, 0)
        before = self.path.read_bytes()
        with mock.patch.object(self.store, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.mutate("edit", "one", {"title": "Changed"}, first["revision"])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
