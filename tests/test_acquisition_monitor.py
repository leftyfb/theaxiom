import unittest
from unittest.mock import patch
from theaxiom import acquisition_monitor as m
from theaxiom.admin_api import WIKIPEDIA_SOURCE


class MonitorTests(unittest.TestCase):
    def test_rate_and_stale(self):
        samples = iter([dict(state="downloading", downloaded_bytes=n,
                             expected_bytes=1000) for n in (100, 200, 200)])
        monitor = m.Monitor(WIKIPEDIA_SOURCE, lambda _: next(samples))
        with patch.object(m.time, "monotonic", side_effect=[1, 3, 3, 5, 5, 20]):
            monitor.sample()
            monitor.sample()
            self.assertEqual(monitor.snapshot()["acquisitions"][0]["speed_bytes_per_second"], 50)
            monitor.sample()
            self.assertIsNone(monitor.snapshot()["acquisitions"][0]["eta_seconds"])
            self.assertTrue(monitor.snapshot()["stale"])

    def test_empty_snapshot(self):
        self.assertTrue(m.Monitor(WIKIPEDIA_SOURCE).snapshot()["stale"])

    @patch.object(m, "unit_state", return_value={"LoadState": "not-found"})
    def test_missing_service(self, _):
        self.assertEqual(m.collect(WIKIPEDIA_SOURCE)["state"], "unavailable")

    @patch.object(m, "unit_state", return_value={"LoadState": "loaded", "ActiveState": "activating"})
    @patch.object(m.storage_settings, "load_settings", return_value={"storage_root": "/tmp"})
    @patch.object(m, "metadata")
    def test_wrong_storage_does_not_stat(self, metadata, *_):
        self.assertEqual(m.collect(WIKIPEDIA_SOURCE)["state"], "unknown")
        metadata.assert_not_called()

    @patch.object(m, "command", return_value="")
    def test_missing_journal_evidence(self, _):
        self.assertFalse(m.success_evidence({"InvocationID": "a"*32}, WIKIPEDIA_SOURCE, None, "/unused"))


class ChecksumProgressTests(unittest.TestCase):
    def test_live_descriptor_position_and_identity(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.zim.part"
            archive.write_bytes(b"x" * 100)
            proc = root / "123"
            (proc / "fd").mkdir(parents=True)
            (proc / "fdinfo").mkdir()
            (proc / "comm").write_text("sha256sum\n")
            (proc / "fd" / "3").symlink_to(archive)
            (proc / "fdinfo" / "3").write_text("pos:\t81\n")
            with patch.object(m, "process_ids", return_value={"123"}):
                candidates = [(archive, archive.stat(), 1, 2)]
                self.assertEqual(m.checksum_progress({}, candidates, 100, root),
                                 {"checksum_bytes": 81, "checksum_pass": 1, "checksum_passes": 2})
                self.assertEqual(m.checksum_progress({}, candidates, 99, root), {})
                (proc / "fdinfo" / "3").unlink()
                self.assertEqual(m.checksum_progress({}, candidates, 100, root), {})
