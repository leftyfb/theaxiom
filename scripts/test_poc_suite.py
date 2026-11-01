#!/usr/bin/env python3
"""Run the standalone repository's pytest suite or check its local test inventory."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def self_test() -> int:
    required = (ROOT / "pyproject.toml", ROOT / "tests", ROOT / "src/theaxiom")
    assert all(path.exists() for path in required), "standalone test layout is incomplete"
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    assert test_files, "no tests found"
    forbidden = ("playbooks/" + "servers.yaml", "playbooks/" + "unifi.yaml", "api." + "kiwix.org")
    source = Path(__file__).read_text(encoding="utf-8")
    assert all(marker not in source for marker in forbidden)
    print(f"Standalone test inventory: {len(test_files)} pytest modules; local-only self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="validate the repository test layout only")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
