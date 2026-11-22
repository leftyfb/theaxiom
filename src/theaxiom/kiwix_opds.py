"""Bounded, read-only parser for Kiwix OPDS 1.2 (Atom XML) acquisition feeds.

This module does NOT fetch URLs, authenticate a catalog, download Metalinks,
derive SHA-256 values, or authorize updates. The caller must use a separate
allowlisted, TLS-verified, rate-limited transport before trusting its input.
"""
from __future__ import annotations

from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 100
DOWNLOAD_HOSTS = frozenset({
    "download.kiwix.org", "lb.download.kiwix.org", "lbo.download.kiwix.org",
})


def parse_acquisition_feed(raw: bytes) -> list[dict[str, str]]:
    """Extract bounded catalog references; no output is an approved update.

    Return a list of publisher-advertised references only. Metalink links
    require a separate verification pipeline; a .meta4 suffix must never be
    stripped to manufacture a direct archive URL.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Invalid OPDS feed size")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD and XML entities are not permitted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Invalid OPDS XML") from exc
    if root.tag != ATOM + "feed":
        raise ValueError("Expected OPDS Atom feed")
    entries = root.findall(ATOM + "entry")
    if len(entries) > MAX_ENTRIES:
        raise ValueError("Too many catalog entries")
    output = []
    for entry in entries:
        title = entry.findtext(ATOM + "title")
        entry_id = entry.findtext(ATOM + "id")
        if (not isinstance(title, str) or not title.strip() or len(title) > 512
                or not isinstance(entry_id, str) or not entry_id.strip()
                or len(entry_id) > 256):
            continue
        for link in entry.findall(ATOM + "link"):
            if (link.get("rel") != ACQUISITION_REL
                    or link.get("type") != "application/x-zim"):
                continue
            href = link.get("href")
            if not isinstance(href, str) or len(href) > 2048:
                continue
            try:
                url = urlsplit(href)
                if (url.scheme != "https" or not url.hostname
                        or url.hostname.lower() not in DOWNLOAD_HOSTS
                        or url.username or url.password or url.fragment
                        or url.port not in (None, 443)
                        or url.query):
                    continue
            except ValueError:
                continue
            path = url.path
            if path.endswith(".zim.meta4"):
                kind = "metalink"
            elif path.endswith(".zim"):
                kind = "direct"
            else:
                continue
            output.append({
                "entry_id": entry_id.strip(),
                "title": title.strip(),
                "acquisition_url": href,
                "acquisition_kind": kind,
                # A feed link alone never establishes an artifact digest.
                "verification": "unverified",
            })
    return output
