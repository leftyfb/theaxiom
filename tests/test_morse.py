"""Deterministic regression checks for the browser-only Morse utility."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_morse_javascript():
    subprocess.run(["node", "--test", str(ROOT / "tests/morse.test.cjs")],
                   cwd=ROOT, check=True, timeout=30)
