#!/usr/bin/env python3
"""Verify the deterministic repository-local POC content fixture."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "kiwix" / "tiny.txt"
README = FIXTURE.parent / "README.md"
MAX_BYTES = 10 * 1024 * 1024
EXPECTED_SHA256 = "d7f3147ded63e205af5a5aecd1ee99050bfd8c8895492520b2c0a5c656a91056"


def main() -> int:
    assert FIXTURE.is_file()
    assert README.is_file()
    data = FIXTURE.read_bytes()
    assert 0 < len(data) <= MAX_BYTES
    digest = hashlib.sha256(data).hexdigest()
    assert digest == EXPECTED_SHA256
    readme = README.read_text(encoding="utf-8")
    assert "deterministic" in readme
    assert "not downloaded from Kiwix" in readme
    print(f"tiny fixture: {len(data)} bytes, sha256={digest}, bound={MAX_BYTES} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
