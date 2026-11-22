"""Safe, explicit Kiwix edition discovery without archive acquisition.

Uses the official fixed OPDS endpoint and only Metalink references published
there. A successful catalog fetch does not mean an archive is installed,
downloaded, or authorized for automatic replacement.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import freshness_probe, kiwix_metadata_transport, kiwix_opds

CATALOG_URL = "https://opds.library.kiwix.org/catalog/v2/entries"
UNKNOWN = {"state": "unable_to_determine", "evidence": "publisher candidate could not be verified"}


def check_kiwix_update(installed: dict, *, fetch=None) -> dict:
    """Check one verified source using at most one catalog and one Metalink GET.

    The installed identity comes from a trusted verified job/manifest and is
    never built from the catalog or a user-edited source card. Only the
    transport's pinned TLS, checked DNS and allowlisted endpoints are trusted.
    """
    if not isinstance(installed, dict) or installed.get("verified") is not True:
        return dict(UNKNOWN)
    family = installed.get("family")
    edition = installed.get("edition")
    if (not isinstance(family, str) or not re.fullmatch(r"[a-z0-9_]{3,120}", family)
            or not isinstance(edition, str)
            or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", edition)
            or installed.get("publisher") != "kiwix"
            or not isinstance(installed.get("language"), str)
            or not installed.get("language")
            or not isinstance(installed.get("source_url"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", str(installed.get("sha256", "")))):
        return dict(UNKNOWN)
    # Caller-supplied fetch is only intended for deterministic test fixtures.
    fetch = kiwix_metadata_transport.fetch_metadata if fetch is None else fetch
    try:
        raw = fetch(CATALOG_URL, kind="catalog")
        candidates = []
        for entry in kiwix_opds.parse_acquisition_feed(raw):
            if entry["acquisition_kind"] != "metalink":
                continue
            path = urlsplit(entry["acquisition_url"]).path
            name = path.rsplit("/", 1)[-1]
            match = re.fullmatch(re.escape(family) + r"_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim\.meta4", name)
            if match and match.group(1) > edition:
                candidates.append((match.group(1), entry))
        if not candidates:
            # A bounded feed can be partial or paginated. Absence isn't proof
            # that the installed archive is the latest edition.
            return dict(UNKNOWN)
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        newest = candidates[0][0]
        # Ambiguous duplicate records must not result in an arbitrary choice.
        if sum(version == newest for version, _ in candidates) != 1:
            return dict(UNKNOWN)
        entry = candidates[0][1]
        link = entry["acquisition_url"]
        name = urlsplit(link).path.rsplit("/", 1)[-1][:-6]
        metadata = kiwix_opds.parse_metalink(
            fetch(link, kind="metalink"), expected_filename=name,
        )
        candidate = kiwix_opds.resolve_catalog_candidate(entry, metadata, installed=installed)
        return freshness_probe.compare_catalog_editions(
            installed, candidate, trusted_catalog=True,
        )
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        # Never convert malformed, unavailable or ambiguous publisher
        # metadata into an update notification or an acquisition action.
        return dict(UNKNOWN)
