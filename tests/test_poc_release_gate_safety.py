#!/usr/bin/env python3
"""Static safety guard for the standalone POC release-gate command graph."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/poc-release-gate.py"
FORBIDDEN = (
    "playbooks/servers.yaml",
    "playbooks/unifi.yaml",
    "ssh",
    "curl",
    "wget",
    "api.kiwix.org",
    "download.kiwix.org",
    "systemctl",
    "ansible-playbook",
)
GRAPH = (
    GATE,
    ROOT / "scripts/test_poc_suite.py",
    ROOT / "scripts/poc-release-readiness.py",
    ROOT / "scripts/poc-release-manifest.py",
    ROOT / "scripts/poc-release-manifest-verify.py",
)


def test_poc_release_gate_safety() -> None:
    main()


def main() -> int:
    for path in GRAPH:
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        assert not any(token in lowered for token in FORBIDDEN), (
            f"forbidden release-gate token in {path.name}"
        )
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
            ):
                assert not any(
                    isinstance(arg, (ast.JoinedStr, ast.FormattedValue)) for arg in node.args
                ), f"dynamic command in {path.name}"
    assert all(path.is_file() for path in GRAPH)
    print("POC release gate safety: standalone static command graph guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
