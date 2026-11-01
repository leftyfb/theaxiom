"""Execute migrated repository-owned checks that predate pytest conventions."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"


def _legacy_entrypoints() -> list[Path]:
    entrypoints = []
    for path in sorted(TEST_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main" for node in tree.body):
            entrypoints.append(path)
    return entrypoints


@pytest.mark.parametrize("script", _legacy_entrypoints(), ids=lambda path: path.name)
def test_legacy_script_entrypoint(script: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(path) for path in (ROOT / "src", ROOT / "src/theaxiom", ROOT / "scripts")
        )
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{script.name} failed:\n{result.stdout}\n{result.stderr}"
