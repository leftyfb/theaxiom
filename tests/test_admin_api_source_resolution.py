#!/usr/bin/env python3
"""C02: verify that only the configured Wikipedia source can be resolved.

Synthetic test — no network, no host access. Proves the trusted manifest
in admin_api.py is the sole resolution path for source IDs.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from theaxiom.admin_api import (
    resolve_source,
    TRUSTED_SOURCES,
    WIKIPEDIA_SOURCE,
)


def test_known_source_resolves() -> None:
    entry = resolve_source("wikipedia-en")
    assert entry is not None, "wikipedia-en must resolve"
    assert entry["source_url"] == WIKIPEDIA_SOURCE["source_url"]
    assert entry["sha256"] == WIKIPEDIA_SOURCE["sha256"]
    assert entry["bytes"] == WIKIPEDIA_SOURCE["bytes"]
    assert entry["filename"] == "wikipedia_en_all_maxi_2026-08.zim"


def test_unknown_source_returns_none() -> None:
    for bogus in ("", "wikipedia", "en", "kiwix", "download", "0xdeadbeef"):
        assert resolve_source(bogus) is None, f"unexpected resolution for {bogus!r}"


def test_trusted_sources_map_has_single_entry() -> None:
    assert len(TRUSTED_SOURCES) == 1
    assert "wikipedia-en" in TRUSTED_SOURCES


def test_source_url_is_https_kiwix() -> None:
    url = WIKIPEDIA_SOURCE["source_url"]
    assert url.startswith("https://"), f"source_url must be HTTPS: {url}"
    assert "download.kiwix.org" in url


def test_sha256_is_64_hex() -> None:
    sha = WIKIPEDIA_SOURCE["sha256"]
    assert len(sha) == 64, f"SHA-256 must be 64 hex chars: {sha}"
    assert all(c in "0123456789abcdef" for c in sha), f"non-hex in SHA-256: {sha}"


def test_bytes_is_positive_int() -> None:
    b = WIKIPEDIA_SOURCE["bytes"]
    assert isinstance(b, int) and b > 0, f"bytes must be positive int: {b}"


if __name__ == "__main__":
    test_known_source_resolves()
    print("PASS: known source 'wikipedia-en' resolves with correct URL/checksum/bytes")
    test_unknown_source_returns_none()
    print("PASS: unknown source IDs all return None")
    test_trusted_sources_map_has_single_entry()
    print("PASS: TRUSTED_SOURCES contains exactly one entry")
    test_source_url_is_https_kiwix()
    print("PASS: source URL is HTTPS on download.kiwix.org")
    test_sha256_is_64_hex()
    print("PASS: SHA-256 is 64 lowercase hex characters")
    test_bytes_is_positive_int()
    print("PASS: byte count is a positive integer")
    print("\nC02 admin API source resolution: ALL PASS")
