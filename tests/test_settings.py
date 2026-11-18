import json
import tempfile
import unittest
from pathlib import Path

from theaxiom.storage_settings import DEFAULT_SETTINGS, load_settings, save_settings, validate_settings


class StorageSettingsTests(unittest.TestCase):
    def test_missing_file_returns_independent_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            first = load_settings(path)
            first["directories"].append("custom")
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)

    def test_save_load_round_trip_and_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            settings = dict(DEFAULT_SETTINGS, storage_source="/dev/test", minimum_free_bytes=4096)
            saved = save_settings(settings, path)
            self.assertEqual(saved, settings)
            self.assertEqual(load_settings(path), settings)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_save_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_settings(DEFAULT_SETTINGS, path)
            before = path.read_bytes()
            invalid = dict(DEFAULT_SETTINGS, storage_mount="/../escape")
            with self.assertRaises(ValueError):
                save_settings(invalid, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(before), DEFAULT_SETTINGS)

    def test_validation_rejects_extra_fields_and_boolean_byte_count(self):
        with self.assertRaises(ValueError):
            validate_settings(dict(DEFAULT_SETTINGS, extra=True))
        with self.assertRaises(ValueError):
            validate_settings(dict(DEFAULT_SETTINGS, minimum_free_bytes=True))


if __name__ == "__main__":
    unittest.main()
