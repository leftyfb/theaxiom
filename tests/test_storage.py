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
