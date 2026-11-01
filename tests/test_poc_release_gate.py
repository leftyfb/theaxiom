#!/usr/bin/env python3
"""Focused test for the local POC release gate entry point."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/poc-release-gate.py"


def main() -> int:
    result = subprocess.run(["python3", str(GATE), "--self-test"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    print("POC release gate: dependency graph and safety self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
