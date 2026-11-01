#!/usr/bin/env python3
"""Verify the offline-library Kiwix metadata path normalization contract."""

from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kiwix-relative-path.yaml"
BASE = "https://download.kiwix.org/zim"


def normalize(path: str) -> str:
    prefix = "/data/zim/"
    if not path.startswith(prefix):
        raise ValueError(f"unsupported Kiwix metadata path: {path}")
    return f"{BASE}/{path[len(prefix):]}"


def read_fixture() -> dict[str, str]:
    values = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and ": " in line:
            key, value = line.split(": ", 1)
            values[key] = value
    return values


def main() -> None:
    fixture = read_fixture()
    assert normalize(fixture["metadata_path"]) == fixture["normalized_url"]
    assert normalize("/data/zim/foo/bar.zim") == f"{BASE}/foo/bar.zim"
    try:
        normalize("/tmp/example.zim")
    except ValueError:
        pass
    else:
        raise AssertionError("non-Kiwix paths must not be normalized")
    print("kiwix path normalization: PASS")


if __name__ == "__main__":
    main()
