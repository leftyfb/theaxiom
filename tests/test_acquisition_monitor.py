import json
import unittest
from types import SimpleNamespace
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

    def test_external_verified_zim_selects_immutable_service_capability(self):
        from theaxiom import source_catalog
        second = dict(WIKIPEDIA_SOURCE, source_id="another-zim")
        with (patch.object(source_catalog, "LEGACY_ACQUISITION_UNITS",
                           {"another-zim": "acquire-another-zim.service"}),
              patch.object(m, "unit_state", return_value={"LoadState": "not-found"}) as unit):
            self.assertEqual(m.collect(second)["state"], "unavailable")
            unit.assert_called_once_with("acquire-another-zim.service")
        with (patch.object(source_catalog, "LEGACY_ACQUISITION_UNITS",
                           {"another-zim": "--invalid.service"}),
              patch.object(m, "unit_state") as unit):
            self.assertEqual(m.collect(second)["state"], "unknown")
            unit.assert_not_called()

    def test_external_service_inspection_is_not_tied_to_wikipedia_unit(self):
        with patch.object(m, "command", return_value="LoadState=not-found\n") as command:
            assert m.unit_state("other-archive.service") == {"LoadState": "not-found"}
            self.assertIn("other-archive.service", command.call_args.args)


    @patch.object(m, "command", return_value="")
    def test_missing_journal_evidence(self, _):
        self.assertFalse(m.success_evidence({"InvocationID": "a"*32}, WIKIPEDIA_SOURCE, None, "/unused"))

    def test_pinned_invocation_evidence_returns_normalized_marker(self):
        invocation = "a" * 32
        unit = {"InvocationID": invocation, "ActiveState": "inactive",
                "Result": "success", "ExecMainStatus": "0"}
        final_stat = SimpleNamespace(st_mtime_ns=10_000_000_000, st_ctime_ns=10_000_000_000)
        path = "/media/offline/library/zim/pinned.zim"
        marker = (f"VERIFIED_AND_PROMOTED path={path} bytes={WIKIPEDIA_SOURCE['bytes']} "
                  f"sha256={WIKIPEDIA_SOURCE['sha256']}")
        with (patch.object(m, "unit_state", side_effect=[unit, unit]),
              patch.object(m, "command", return_value=json.dumps(
                  {"MESSAGE": marker, "__REALTIME_TIMESTAMP": "11000000"}) + "\n") as command):
            evidence = m.pinned_invocation_evidence(
                WIKIPEDIA_SOURCE, path, final_stat, unit)
        self.assertEqual(evidence, {"timestamp": 11.0, "timestamp_ns": 11000000000,
                                    "marker": marker, "invocation_id": invocation})
        self.assertIn("-n", command.call_args.args)
        self.assertIn("30", command.call_args.args)
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + invocation, command.call_args.args)

    def test_pinned_invocation_evidence_requires_strictly_newer_timestamp(self):
        invocation = "b" * 32
        unit = {"InvocationID": invocation, "ActiveState": "inactive",
                "Result": "success", "ExecMainStatus": "0"}
        marker = ("VERIFIED_AND_PROMOTED path=/exact.zim bytes=7 sha256=" + "c" * 64)
        final_stat = SimpleNamespace(st_mtime_ns=10_000_000_000, st_ctime_ns=10_000_000_000)
        with (patch.object(m, "unit_state", side_effect=[unit, unit]),
              patch.object(m, "command", return_value=json.dumps(
                  {"MESSAGE": marker, "__REALTIME_TIMESTAMP": "10000000"}))):
            self.assertIsNone(m.pinned_invocation_evidence(
                {"bytes": 7, "sha256": "c" * 64}, "/exact.zim", final_stat, unit))

    def test_pinned_invocation_evidence_rejects_unit_change_after_query(self):
        invocation = "d" * 32
        unit = {"InvocationID": invocation, "ActiveState": "inactive",
                "Result": "success", "ExecMainStatus": "0"}
        changed = dict(unit, InvocationID="e" * 32)
        with (patch.object(m, "unit_state", side_effect=[unit, changed]),
              patch.object(m, "command", return_value="")):
            self.assertIsNone(m.pinned_invocation_evidence(
                {"bytes": 1, "sha256": "f" * 64}, "/exact.zim",
                SimpleNamespace(st_mtime_ns=1_000_000_000, st_ctime_ns=1_000_000_000), unit))


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
                self.assertEqual(m.checksum_progress({}, [(archive, archive.stat(), 2, 2)], 100, root),
                                 {"checksum_bytes": 81, "checksum_pass": 2, "checksum_passes": 2})
                self.assertEqual(m.checksum_progress({}, candidates, 99, root), {})
                (proc / "fdinfo" / "3").unlink()
                self.assertEqual(m.checksum_progress({}, candidates, 100, root), {})
