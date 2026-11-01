#!/usr/bin/env python3
"""Focused tests for the local POC release-manifest verifier."""

from __future__ import annotations
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts/poc-release-manifest-verify.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "fixture.txt"
        artifact.write_text("offline fixture\n", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "path": "fixture.txt",
                            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        def run():
            return subprocess.run(
                ["python3", str(VERIFY), "--manifest", str(manifest), "--root", str(root)],
                capture_output=True,
                text=True,
            )

        result = run()
        assert result.returncode == 0, result.stdout + result.stderr
        artifact.write_text("changed\n", encoding="utf-8")
        result = run()
        assert result.returncode != 0 and "checksum mismatch: fixture.txt" in result.stdout
        artifact.unlink()
        result = run()
        assert result.returncode != 0 and "missing artifact: fixture.txt" in result.stdout
    print("POC release manifest verifier: missing and mismatched artifacts rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
