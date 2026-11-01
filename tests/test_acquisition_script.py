"""Local-only tests for the configurable, checksum-gated acquisition helper."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/acquire_kiwix_zim.sh"


def test_acquisition_helper_uses_configured_staging_root(tmp_path: Path) -> None:
    staging = tmp_path / "library" / "staging"
    env = {**os.environ, "THEAXIOM_STAGING_DIR": str(staging)}
    result = subprocess.run(
        ["bash", str(SCRIPT), "https://example.invalid/file", str(staging / "file.zim"), "bad", "a" * 64],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "invalid expected byte count" in result.stderr


def test_acquisition_helper_rejects_path_outside_configured_staging_root(tmp_path: Path) -> None:
    env = {**os.environ, "THEAXIOM_STAGING_DIR": str(tmp_path / "staging")}
    result = subprocess.run(
        ["bash", str(SCRIPT), "https://example.invalid/file", "/media/offline/staging/file.zim", "1", "a" * 64],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "destination must be under" in result.stderr
