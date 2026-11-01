#!/usr/bin/env python3
"""Run the local offline-library release-readiness and manifest gates."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READINESS = ROOT / "scripts/poc-release-readiness.py"
MANIFEST = ROOT / "scripts/poc-release-manifest.py"
VERIFY = ROOT / "scripts/poc-release-manifest-verify.py"
SUITE = ROOT / "scripts/test_poc_suite.py"


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def self_test() -> int:
    source = Path(__file__).read_text(encoding="utf-8")
    for marker in ("playbooks/" + "servers.yaml", "playbooks/" + "unifi.yaml", "s" + "sh", "cu" + "rl", "wg" + "et"):
        assert marker not in source, f"unsafe release-gate reference: {marker}"
    assert SUITE.is_file() and READINESS.is_file() and MANIFEST.is_file() and VERIFY.is_file()
    print("POC release gate self-test: local dependency graph and safety checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--readiness", type=Path, default=ROOT / "fixtures/kiwix/poc-release-readiness.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "fixtures/kiwix/poc-release-manifest.json")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    run([sys.executable, str(SUITE), "--self-test"])
    run([sys.executable, str(READINESS), "--output", str(args.readiness)])
    run([sys.executable, str(MANIFEST), "--output", str(args.manifest)])
    run([sys.executable, str(VERIFY), "--manifest", str(args.manifest), "--root", str(ROOT)])
    print("POC release gate: readiness and manifest verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
