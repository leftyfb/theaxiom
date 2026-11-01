#!/usr/bin/env python3
"""Unit tests for freshness results using only mocked HTTP responses."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/theaxiom"))

import freshness_probe  # noqa: E402


class Response:
    status = 200
    headers = {"Content-Length": "754723", "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


class FreshnessProbeTests(unittest.TestCase):
    def test_matching_manifest_metadata_is_current_without_network(self):
        opener = mock.Mock(return_value=Response())
        result = freshness_probe.probe_source(
            "https://example.invalid/document", "trusted-sha256", 754723, opener=opener
        )

        self.assertTrue(result["reachable"])
        self.assertTrue(result["local_current"])
        self.assertEqual(result["content_length"], "754723")
        self.assertEqual(result["last_modified"], Response.headers["Last-Modified"])
        opener.assert_called_once()

    def test_mismatched_length_is_stale_and_trusted_metadata_is_passed(self):
        opener = mock.Mock(return_value=Response())
        docs = [{
            "id": "trusted-id",
            "title": "Trusted title",
            "source_url": "https://example.invalid/document",
            "sha256": "trusted-sha256",
            "bytes": 1,
        }]

        result = freshness_probe.probe_documents(docs, opener=opener)[0]

        self.assertTrue(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertEqual(result["id"], "trusted-id")
        self.assertEqual(result["title"], "Trusted title")
        opener.assert_called_once()

    def test_probe_error_is_reported_without_network(self):
        opener = mock.Mock(side_effect=OSError("offline"))
        result = freshness_probe.probe_source(
            "https://example.invalid/document", "trusted-sha256", 754723, opener=opener
        )

        self.assertFalse(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertIn("offline", result["error"])
        opener.assert_called_once()

    def test_missing_source_url_is_reported_without_calling_opener(self):
        opener = mock.Mock()
        result = freshness_probe.probe_documents(
            [{"id": "no-url", "title": "No URL", "sha256": "trusted-sha256", "bytes": 123}],
            opener=opener,
        )[0]

        self.assertFalse(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertEqual(result["error"], "no source_url in manifest")
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
