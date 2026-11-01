#!/usr/bin/env python3
"""Verify that every artifact in a local POC release manifest is present and pinned."""

from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(manifest_path: Path, root: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    for item in manifest.get("artifacts", []):
        relative, expected = item.get("path", ""), item.get("sha256", "")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            errors.append(f"unsafe artifact path: {relative}")
            continue
        if not path.is_file():
            errors.append(f"missing artifact: {relative}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"checksum mismatch: {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "fixtures/kiwix/poc-release-manifest.json"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors = verify(args.manifest, args.root)
    if errors:
        print("\n".join(errors))
        return 1
    print("POC release manifest: all artifacts present and checksums validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
