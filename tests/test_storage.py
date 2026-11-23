import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from theaxiom import storage
from theaxiom.storage_settings import DEFAULT_SETTINGS


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.config = dict(DEFAULT_SETTINGS, storage_root=str(self.root))
        self.mount = {"target": "/", "source": "/dev/test", "type": "ext4",
                      "device": "1:2", "read_only": False}

    def inspect(self, config=None, mounts=None, **kwargs):
        with patch.object(storage, "read_mounts", return_value=mounts or [self.mount]):
            return storage.inspect_storage(config or self.config, **kwargs)

    def test_unconfigured(self):
        self.assertEqual(self.inspect(DEFAULT_SETTINGS)["error"], "storage_not_configured")

    def test_local_root(self):
        result = self.inspect()
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["paths"]["library"], str(self.root / "library/zim"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_required_mount_never_probes_capacity(self):
        config = dict(self.config, storage_mount=str(self.root), storage_source="server:/export")
        with patch.object(storage.os, "statvfs") as probe:
            result = self.inspect(config)
        self.assertEqual(result["error"], "required_mount_missing_or_replaced")
        probe.assert_not_called()

    def test_replaced_mount(self):
        config = dict(self.config, storage_mount=str(self.root), storage_source="server:/export")
        other = dict(self.mount, target=str(self.root), source="other:/export")
        self.assertEqual(self.inspect(config, [self.mount, other])["error"],
                         "required_mount_missing_or_replaced")

    def test_expected_mount(self):
        config = dict(self.config, storage_mount=str(self.root), storage_source="server:/export")
        expected = dict(self.mount, target=str(self.root), source="server:/export", type="nfs4")
        self.assertTrue(self.inspect(config, [self.mount, expected])["ready"])

    def test_read_only(self):
        self.assertEqual(self.inspect(mounts=[dict(self.mount, read_only=True)])["error"],
                         "read_only_filesystem")

    def test_permission_failure(self):
        with patch.object(storage.os, "access", return_value=False):
            self.assertEqual(self.inspect()["error"], "storage_not_writable")

    def test_capacity(self):
        self.assertEqual(self.inspect(required_bytes=2**80)["error"], "insufficient_capacity")

    def test_symlink(self):
        (self.root / "staging").symlink_to("/tmp", target_is_directory=True)
        self.assertEqual(self.inspect()["error"], "symlink_not_allowed")

    def test_nested_mount(self):
        nested = dict(self.mount, target=str(self.root / "library"))
        self.assertEqual(self.inspect(mounts=[self.mount, nested])["error"],
                         "nested_or_replaced_mount")

    def test_mount_escape(self):
        self.assertEqual(storage._decode(r"/media/my\040disk"), "/media/my disk")


def test_capacity_io_error_is_classified_and_does_not_echo_exception(tmp_path, monkeypatch):
    import errno
    import os

    def stale(path):
        raise OSError(errno.ESTALE, "remote detail", "/private/server/path")

    monkeypatch.setattr(os, "statvfs", stale)
    result = storage.inspect_storage(dict(DEFAULT_SETTINGS, storage_root=str(tmp_path)))
    assert not result["ready"]
    assert result["error"] == "stale_storage_handle"
    assert result["operation"] == "check_capacity"
    assert result["failed_path"] == str(tmp_path)
    assert "remote detail" not in str(result)


def test_blocked_probe_does_not_start_another_or_report_previous_root(tmp_path, monkeypatch):
    import threading

    entered, release, finished = (threading.Event() for _ in range(3))
    real_check = storage.inspect_storage
    checks = []

    def check(settings, *, progress):
        checks.append(settings["storage_root"])
        if len(checks) == 1:
            progress("check_capacity", settings["storage_root"])
            entered.set()
            release.wait(5)
        return real_check(settings, progress=progress)

    monkeypatch.setattr(storage, "inspect_storage", check)
    probe = storage.StorageProbe()
    inspect = probe._inspect

    def inspect_and_signal(run):
        inspect(run)
        finished.set()

    monkeypatch.setattr(probe, "_inspect", inspect_and_signal)
    cfg = dict(DEFAULT_SETTINGS, storage_root=str(tmp_path))
    other = dict(cfg, storage_root=str(tmp_path / "other"))
    try:
        assert probe.status(cfg)["pending"]
        assert entered.wait(2)
        monkeypatch.setattr(probe, "STALL_AFTER", 0)
        result = probe.status(cfg)
        assert result["error"] == "storage_probe_timeout"
        assert result["operation"] == "check_capacity"
        assert result["failed_path"] == str(tmp_path)
        assert probe.status(other).get("failed_path") is None
        assert checks == [str(tmp_path)]
    finally:
        release.set()
        assert finished.wait(2)
    assert not probe.status(cfg).get("pending")
