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
    def test_matching_origin_length_does_not_prove_local_installation(self):
        opener = mock.Mock(return_value=Response())
        result = freshness_probe.probe_source(
            "https://example.invalid/document", "trusted-sha256", 754723, opener=opener
        )

        self.assertTrue(result["reachable"])
        self.assertFalse(result["local_current"])
        self.assertTrue(result["origin_length_matches_manifest"])
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
        self.assertFalse(result["origin_length_matches_manifest"])
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
        self.assertIsNone(result["origin_length_matches_manifest"])
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
        self.assertIsNone(result["origin_length_matches_manifest"])
        self.assertEqual(result["error"], "no source_url in manifest")
        opener.assert_not_called()

    def test_missing_length_does_not_claim_origin_match_or_local_installation(self):
        response = Response()
        response.headers = {"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        result = freshness_probe.probe_source(
            "https://example.invalid/document", "trusted-sha256", 754723,
            opener=mock.Mock(return_value=response))
        self.assertTrue(result["reachable"])
        self.assertIsNone(result["origin_length_matches_manifest"])
        self.assertFalse(result["local_current"])

    def test_malformed_length_is_unknown_not_a_server_error(self):
        response = Response()
        response.headers = {"Content-Length": "not-a-number"}
        result = freshness_probe.probe_source(
            "https://example.invalid/document", "trusted-sha256", 754723,
            opener=mock.Mock(return_value=response))
        self.assertIsNone(result["origin_length_matches_manifest"])
        self.assertFalse(result["local_current"])



class InstalledEvidenceTests(unittest.TestCase):
    ENTRY = {"source_id": "wikipedia-en", "filename": "wikipedia.zim",
             "bytes": 754723, "sha256": "a" * 64}

    def job(self, **overrides):
        job = {"source_id": self.ENTRY["source_id"], "filename": self.ENTRY["filename"],
               "expected_bytes": self.ENTRY["bytes"], "sha256": self.ENTRY["sha256"],
               "state": "completed", "verified": True, "active": False,
               "downloaded_bytes": self.ENTRY["bytes"]}
        job.update(overrides)
        return job

    def classify(self, job, *, stale=False):
        return freshness_probe.installed_evidence(
            {"stale": stale, "acquisitions": [job]}, self.ENTRY)

    def test_verified_exact_manifest_and_fresh_snapshot(self):
        result = self.classify(self.job())
        self.assertTrue(result["local_current"])
        self.assertTrue(result["installed_verified"])
        self.assertEqual(result["installed_state"], "verified")

    def test_completed_job_with_short_or_unknown_byte_count_is_not_verified(self):
        for count in (None, 0, 754722, 754724, "754723"):
            with self.subTest(count=count):
                result = self.classify(self.job(downloaded_bytes=count))
                self.assertFalse(result["local_current"])
                self.assertFalse(result["installed_verified"])

    def test_boolean_byte_counts_are_not_verified(self):
        for field in ("downloaded_bytes", "expected_bytes"):
            with self.subTest(field=field):
                entry = dict(self.ENTRY, bytes=1)
                job = self.job(expected_bytes=1, downloaded_bytes=1)
                job[field] = True
                result = freshness_probe.installed_evidence(
                    {"stale": False, "acquisitions": [job]}, entry)
                self.assertFalse(result["installed_verified"])
                self.assertEqual(result["installed_state"], "unknown")

    def test_invalid_manifest_identity_never_verifies(self):
        for invalid in (None, True, 0, -1, "754723"):
            with self.subTest(bytes=invalid):
                entry = dict(self.ENTRY, bytes=invalid)
                result = freshness_probe.installed_evidence(
                    {"stale": False, "acquisitions": [self.job()]}, entry)
                self.assertFalse(result["installed_verified"])
        for digest in ("", "g" * 64, "A" * 64, None):
            with self.subTest(digest=digest):
                entry = dict(self.ENTRY, sha256=digest)
                result = freshness_probe.installed_evidence(
                    {"stale": False, "acquisitions": [self.job(sha256=digest)]}, entry)
                self.assertFalse(result["installed_verified"])

    def test_missing_manifest_identity_cannot_match_missing_job_identity(self):
        for field in ("source_id", "filename"):
            for value in (None, "", "   ", False, 123):
                with self.subTest(field=field, value=value):
                    entry = dict(self.ENTRY, **{field: value})
                    job = self.job(**{field: value})
                    result = freshness_probe.installed_evidence(
                        {"stale": False, "acquisitions": [job]}, entry)
                    self.assertFalse(result["local_current"])
                    self.assertFalse(result["installed_verified"])
                    self.assertEqual(result["installed_state"], "unknown")

    def test_malformed_state_is_unknown_not_server_error(self):
        for state in ([], {}, 123, None):
            with self.subTest(state=state):
                result = self.classify(self.job(state=state, verified=False))
                self.assertEqual(result["installed_state"], "unknown")
                self.assertFalse(result["installed_verified"])

    def test_absent_file(self):
        result = self.classify(self.job(state="not_started", verified=False,
                                        downloaded_bytes=0))
        self.assertEqual(result["installed_state"], "absent")
        self.assertFalse(result["local_current"])

    def test_partial_download(self):
        result = self.classify(self.job(state="interrupted", verified=False,
                                        downloaded_bytes=100))
        self.assertEqual(result["installed_state"], "partial_or_unverified")
        self.assertFalse(result["local_current"])

    def test_same_size_failed_checksum_is_not_verified(self):
        result = self.classify(self.job(state="failed", verified=False))
        self.assertEqual(result["installed_state"], "failed_or_corrupt")
        self.assertFalse(result["local_current"])

    def test_same_size_without_verification_is_unknown(self):
        result = self.classify(self.job(verified=False))
        self.assertEqual(result["installed_state"], "unknown")
        self.assertFalse(result["local_current"])

    def test_unavailable_storage(self):
        result = self.classify(self.job(state="unavailable", verified=False))
        self.assertEqual(result["installed_state"], "unavailable")
        self.assertFalse(result["local_current"])

    def test_stale_snapshot_never_claims_verified(self):
        result = self.classify(self.job(), stale=True)
        self.assertEqual(result["installed_state"], "unknown")
        self.assertFalse(result["local_current"])

    def test_manifest_identity_mismatch_never_claims_verified(self):
        for field, value in (("filename", "other.zim"), ("expected_bytes", 5),
                             ("sha256", "b" * 64), ("source_id", "other")):
            with self.subTest(field=field):
                result = self.classify(self.job(**{field: value}))
                self.assertFalse(result["local_current"])
                self.assertEqual(result["installed_state"], "unknown")

    def test_active_or_duplicate_jobs_never_claim_verified(self):
        self.assertFalse(self.classify(self.job(active=True))["local_current"])
        snapshot = {"stale": False, "acquisitions": [self.job(), self.job()]}
        self.assertFalse(freshness_probe.installed_evidence(snapshot, self.ENTRY)["local_current"])


class RemoteRevisionEvidenceTests(unittest.TestCase):
    INSTALLED = {
        "verified": True,
        "sha256": "a" * 64,
        "source_url": "https://example.invalid/archive.zim",
        "etag": '"edition-one"',
        "bytes": 100,
    }

    def classify(self, remote, installed=None):
        return freshness_probe.classify_remote_revision(
            self.INSTALLED if installed is None else installed, remote)

    def test_matching_strong_validator_is_same_representation(self):
        self.assertEqual(self.classify({
            "source_url": self.INSTALLED["source_url"], "etag": '"edition-one"'
        })["state"], "representation_unchanged")

    def test_different_strong_validator_does_not_assert_newer_edition(self):
        result = self.classify({
            "source_url": self.INSTALLED["source_url"], "etag": '"edition-two"',
            "content_length": 100,
        })
        self.assertEqual(result["state"], "representation_changed")
        self.assertNotEqual(result["state"], "update_available")

    def test_size_difference_alone_is_never_authoritative(self):
        for size in (100, 101, 99, "101"):
            with self.subTest(size=size):
                self.assertEqual(self.classify({
                    "source_url": self.INSTALLED["source_url"],
                    "content_length": size,
                })["state"], "unable_to_determine")

    def test_weak_etag_timestamp_and_different_url_are_not_authoritative(self):
        cases = [
            {"source_url": self.INSTALLED["source_url"], "etag": 'W/"edition-two"'},
            {"source_url": self.INSTALLED["source_url"], "last_modified": "Tue, 01 Jan 2030 00:00:00 GMT"},
            {"source_url": "https://example.invalid/new.zim", "etag": '"edition-two"'},
            {"source_url": self.INSTALLED["source_url"], "etag": "unquoted"},
        ]
        for remote in cases:
            with self.subTest(remote=remote):
                self.assertEqual(self.classify(remote)["state"], "unable_to_determine")

    def test_unverified_or_unpinned_install_is_not_compared(self):
        for changes in ({"verified": False}, {"sha256": "bad"}, {"verified": None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.classify({
                    "source_url": self.INSTALLED["source_url"], "etag": '"edition-two"'
                }, dict(self.INSTALLED, **changes))["state"], "unable_to_determine")

    def test_malformed_metadata_fails_closed(self):
        for remote in (None, [], "", {"source_url": self.INSTALLED["source_url"], "etag": 4}):
            with self.subTest(remote=remote):
                self.assertEqual(self.classify(remote)["state"], "unable_to_determine")


if __name__ == "__main__":
    unittest.main()
