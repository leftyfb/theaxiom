"""Bounded, read-only parser for Kiwix OPDS 1.2 (Atom XML) acquisition feeds.

This module does NOT fetch URLs, authenticate a catalog, download Metalinks,
derive SHA-256 values, or authorize updates. The caller must use a separate
allowlisted, TLS-verified, rate-limited transport before trusting its input.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 100
DOWNLOAD_HOSTS = frozenset({
    "download.kiwix.org", "lb.download.kiwix.org", "lbo.download.kiwix.org",
})


def parse_acquisition_feed(raw: bytes, *, include_catalog_metadata=False) -> list[dict[str, str]]:
    """Extract bounded catalog references; no output is an approved update.

    Return a list of publisher-advertised references only. Metalink links
    require a separate verification pipeline; a .meta4 suffix must never be
    stripped to manufacture a direct archive URL.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Invalid OPDS feed size")
    if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
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
            result = {
                "entry_id": entry_id.strip(),
                "title": title.strip(),
                "acquisition_url": href,
                "acquisition_kind": kind,
                # A feed link alone never establishes an artifact digest.
                "verification": "unverified",
            }
            if include_catalog_metadata:
                rights = (entry.findtext(ATOM + "rights") or "").strip()
                length = link.get("length", "")
                if rights and len(rights) <= 512:
                    result["rights"] = rights
                if kind == "direct" and length.isascii() and length.isdecimal() and len(length) <= 19:
                    size = int(length)
                    if 96 <= size <= 2**63 - 1:
                        result["advertised_size"] = length
            output.append(result)
    return output


METALINK = "{urn:ietf:params:xml:ns:metalink}"
MAX_METALINK_BYTES = 64 * 1024
MAX_INSPECTION_BYTES = 4 * 1024 * 1024  # Some official Metalinks list tens of thousands of piece hashes.


def _metalink_file(raw, expected_filename, cap):
    if (not isinstance(expected_filename, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,240}\.zim", expected_filename)
            or ".." in expected_filename):
        raise ValueError("Invalid expected ZIM filename")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= cap:
        raise ValueError("Invalid Metalink size")
    if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD and XML entities are not permitted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Invalid Metalink XML") from exc
    if root.tag != METALINK + "metalink":
        raise ValueError("Expected Metalink4 document")
    files = root.findall(METALINK + "file")
    if len(files) != 1 or files[0].get("name") != expected_filename:
        raise ValueError("Metalink file identity mismatch")
    file = files[0]
    size_value = file.findtext(METALINK + "size")
    if (not isinstance(size_value, str) or len(size_value) > 19
            or not size_value.isascii() or not size_value.isdecimal()):
        raise ValueError("Missing trusted size")
    size = int(size_value)
    if not 96 <= size <= 2**63 - 1:
        raise ValueError("Invalid ZIM size")
    sha_values = [item.text for item in file.findall(METALINK + "hash")
                  if item.get("type") == "sha-256"]
    if len(sha_values) != 1 or not isinstance(sha_values[0], str):
        raise ValueError("Missing unambiguous SHA-256")
    sha = sha_values[0].lower()
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("Invalid SHA-256")
    return file, size, sha


def inspect_metalink(raw, *, expected_filename, publisher_url=None):
    """Read catalog evidence and optionally match an exact caller-pinned URL."""
    file, size, sha = _metalink_file(raw, expected_filename, MAX_INSPECTION_BYTES)
    result = {"filename": expected_filename, "bytes": str(size), "sha256": sha}
    if publisher_url and any(link.text == publisher_url for link in file.findall(METALINK + "url")):
        result["source_url"] = publisher_url
    return result


def parse_metalink(
    raw: bytes, *, expected_filename: str, preferred_host: str | None = None,
    catalog_metalink_url: str | None = None,
) -> dict[str, object]:
    """Validate a known Metalink4 file digest and HTTPS mirror without I/O."""
    if preferred_host is not None and preferred_host not in DOWNLOAD_HOSTS:
        raise ValueError("Unapproved preferred Metalink host")
    file, size, sha = _metalink_file(raw, expected_filename, MAX_METALINK_BYTES)
    urls = []
    for link in file.findall(METALINK + "url"):
        href = link.text
        if not isinstance(href, str) or len(href) > 2048:
            continue
        try:
            url = urlsplit(href)
            if (url.scheme != "https" or not url.hostname
                    or url.hostname.lower() not in DOWNLOAD_HOSTS
                    or url.username or url.password or url.fragment or url.query
                    or url.port not in (None, 443)
                    or url.path.rsplit("/", 1)[-1] != expected_filename):
                continue
        except ValueError:
            continue
        urls.append(href)
    # Use an explicit same-host URL when present. A trusted OPDS acquisition
    # link can supply the canonical URL when MirrorBrain lists only mirrors.
    approved = sorted(set(
        url for url in urls
        if preferred_host is None or urlsplit(url).hostname == preferred_host
    ))
    if not approved and preferred_host and catalog_metalink_url:
        location = urlsplit(catalog_metalink_url)
        if (location.scheme == "https" and location.hostname == preferred_host
                and location.netloc in (preferred_host, preferred_host + ":443")
                and not location.query and not location.fragment
                and re.fullmatch(r"/zim/[a-zA-Z0-9._/-]+\.zim\.meta4", location.path)
                and not any(part in {".", "..", ""} for part in location.path[1:].split("/"))
                and location.path.rsplit("/", 1)[-1] == expected_filename + ".meta4"):
            approved.append(location._replace(path=location.path.removesuffix(".meta4")).geturl())
    if len(approved) != 1:
        raise ValueError("No unique same-publisher ZIM URL in Metalink")
    return {
        "filename": expected_filename, "source_url": approved[0],
        "bytes": size, "sha256": sha, "verification": "metadata_only",
    }


def resolve_catalog_candidate(
    entry: dict[str, str],
    metalink: dict[str, object],
    *,
    installed: dict[str, object],
) -> dict[str, object]:
    """Bind a parsed OPDS entry and separately fetched Metalink to one family.

    These values are NOT trusted merely because they parse. The caller must
    obtain both responses via the guarded transport and pin the Metalink URL
    to the exact OPDS acquisition link before calling this function. This is
    a conservative identity check, not an update permission.
    """
    if (not isinstance(entry, dict) or not isinstance(metalink, dict)
            or not isinstance(installed, dict)
            or entry.get("acquisition_kind") != "metalink"
            or entry.get("verification") != "unverified"
            or metalink.get("verification") != "metadata_only"):
        raise ValueError("Unverified or incomplete Kiwix candidate")
    source = entry.get("acquisition_url")
    if not isinstance(source, str):
        raise ValueError("Missing OPDS acquisition link")
    location = urlsplit(source)
    if (location.scheme != "https" or location.hostname not in DOWNLOAD_HOSTS
            or not location.path.endswith(".zim.meta4")
            or location.query or location.fragment):
        raise ValueError("Invalid OPDS acquisition link")
    filename = location.path.rsplit("/", 1)[-1][:-6]  # exact filename expected from the catalog
    if not isinstance(metalink.get("filename"), str) or metalink["filename"] != filename:
        raise ValueError("OPDS and Metalink file identities disagree")
    family = installed.get("family")
    edition = installed.get("edition")
    if (not isinstance(family, str) or not re.fullmatch(r"[a-z0-9._-]{3,120}", family)
            or not isinstance(edition, str) or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", edition)
            or not re.fullmatch(re.escape(family) + r"_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim", filename)):
        raise ValueError("Candidate is outside the installed archive family")
    candidate_edition = filename[len(family) + 1:-4]
    if (not isinstance(metalink.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", metalink["sha256"])
            or type(metalink.get("bytes")) is not int
            or metalink["bytes"] < 96):
        raise ValueError("Candidate lacks a verified metadata pin")
    candidate_url = metalink.get("source_url")
    if not isinstance(candidate_url, str):
        raise ValueError("Missing explicit Metalink download URL")
    parsed_url = urlsplit(candidate_url)
    installed_url = installed.get("source_url")
    if (not isinstance(installed_url, str)
            or parsed_url.hostname != urlsplit(installed_url).hostname
            or parsed_url.path.rsplit("/", 1)[-1] != filename):
        raise ValueError("Candidate crosses the installed publisher host")
    return {
        "family": family, "edition": candidate_edition,
        "language": installed.get("language"), "variant": installed.get("variant"),
        "publisher": installed.get("publisher"),
        "source_url": candidate_url, "sha256": metalink["sha256"],
        "bytes": metalink["bytes"], "verification": "metadata_only",
    }
