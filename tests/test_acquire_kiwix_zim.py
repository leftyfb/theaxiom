import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acquire_kiwix_zim.sh"


class AcquireKiwixZimTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.destination = self.staging / "sample.zim"
        self.payload = b"verified local ZIM fixture\n"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.curl_log = self.root / "curl-args"
        self.payload_file = self.root / "payload"
        self.payload_file.write_bytes(self.payload)
        curl = self.bin_dir / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > "$CURL_LOG"\n'
            'output=\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--output" ]; then shift; output=$1; fi\n'
            '  shift\n'
            'done\n'
            '[ -n "$output" ] || exit 2\n'
            'cp -- "$CURL_PAYLOAD_FILE" "$output"\n'
            'exit "${CURL_EXIT:-0}"\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin_dir}:{self.env['PATH']}",
            CURL_LOG=str(self.curl_log),
            CURL_PAYLOAD_FILE=str(self.payload_file),
            THEAXIOM_STAGING_DIR=str(self.staging),
        )

    def run_script(self, *arguments, env=None):
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            result = run(
                ["bash", str(SCRIPT), *map(str, arguments)],
                capture_output=True,
                text=True,
                env=env or self.env,
                check=False,
            )
            run.assert_called_once()
        return result

    def args(self, *extra):
        return (
            "https://example.invalid/sample.zim",
            self.destination,
            str(len(self.payload)),
            self.digest,
            *extra,
        )

    def assert_no_partial_files(self):
        self.assertEqual(list(self.staging.glob("*.part")), [])
        self.assertEqual(list(self.staging.glob(".acquire-kiwix.*")), [])

    def test_four_arguments_accept_verified_existing_file_without_download(self):
        self.destination.write_bytes(self.payload)
        result = self.run_script(*self.args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already complete", result.stdout)
        self.assertFalse(self.curl_log.exists())
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_missing_destination_downloads_and_installs_verified_file(self):
        result = self.run_script(*self.args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified and installed", result.stdout)
        self.assertTrue(self.curl_log.exists())
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assert_no_partial_files()

    def test_explicit_overwrite_installs_verified_download(self):
        self.destination.write_bytes(b"old bytes")
        result = self.run_script(*self.args("--overwrite"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified and installed", result.stdout)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assert_no_partial_files()

    def test_failed_overwrite_preserves_original_and_cleans_partial(self):
        self.destination.write_bytes(b"original bytes")
        env = self.env | {"CURL_EXIT": "22"}
        result = self.run_script(*self.args("--overwrite"), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_bytes(), b"original bytes")
        self.assert_no_partial_files()

    def test_invalid_overwrite_preserves_original_and_cleans_partial(self):
        self.destination.write_bytes(b"original bytes")
        bad_digest = hashlib.sha256(b"different content").hexdigest()
        result = self.run_script(
            "https://example.invalid/sample.zim", self.destination,
            str(len(self.payload)), bad_digest, "--overwrite",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_bytes(), b"original bytes")
        self.assert_no_partial_files()

    def test_unsupported_argument_is_rejected(self):
        result = self.run_script(*self.args("--force"))
        self.assertEqual(result.returncode, 64)
        self.assertIn("usage:", result.stderr)
        self.assertFalse(self.curl_log.exists())
