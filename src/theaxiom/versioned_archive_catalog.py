"""Metadata-only parsing for versioned archive feeds; no content acquisition.

Provider formats are independent of source IDs. A caller pins the artifact
family, extension and metadata endpoint. Published digests retain their actual
algorithm; they are never represented as SHA-256 unless supplied as such.
"""
from datetime import date
import json
import re
from urllib.parse import urlsplit

MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2**40


def _load(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_METADATA_BYTES:
        raise ValueError("Invalid metadata size")
    return json.loads(raw)


def _candidate(name, size, digest, algorithm, *, family, extension, base, compact=False):
    if not isinstance(name, str) or not isinstance(family, str):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9_-]{0,120}", family) or extension not in ("zim", "pmtiles"):
        raise ValueError("Invalid archive family or format")
    pattern = r"(20\d{6})" if compact else r"(20\d{2}-\d{2}-\d{2})([a-z]?)"
    match = re.fullmatch(re.escape(family) + pattern + r"\." + extension, name)
    if not match:
        return None
    edition = match.group(1)
    if compact:
        edition = f"{edition[:4]}-{edition[4:6]}-{edition[6:]}"
    try:
        date.fromisoformat(edition)
    except ValueError:
        return None
    if not compact:
        edition += match.group(2)
    if isinstance(size, str) and re.fullmatch(r"[1-9][0-9]{0,12}", size):
        size = int(size)
    return {"filename": name, "edition": edition, "bytes": size,
            "source_url": base + name, "digest_algorithm": algorithm, "digest": digest}


def _latest(candidates):
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        raise ValueError("No matching archive editions")
    newest = max(candidate["edition"] for candidate in candidates)
    matching = [candidate for candidate in candidates if candidate["edition"] == newest]
    if len(matching) != 1:
        raise ValueError("Ambiguous newest archive edition")
    candidate = matching[0]
    size, digest = candidate["bytes"], candidate["digest"]
    length = {"sha1": 40, "blake3": 64, "sha256": 64}[candidate["digest_algorithm"]]
    if (type(size) is not int or not 96 <= size <= MAX_ARCHIVE_BYTES or not isinstance(digest, str)
            or not re.fullmatch(r"[a-fA-F0-9]{%d}" % length, digest)):
        raise ValueError("Missing or invalid publisher artifact evidence")
    return dict(candidate, digest=digest.lower())


def internet_archive_latest(raw, *, endpoint, family, extension="zim"):
    """Select original dated files from one pinned Internet Archive item."""
    url = urlsplit(endpoint)
    match = re.fullmatch(r"/metadata/([a-zA-Z0-9_-]{1,128})", url.path)
    if url.scheme != "https" or url.netloc != "archive.org" or not match or url.query or url.fragment:
        raise ValueError("Expected an Internet Archive item metadata endpoint")
    item = match.group(1)
    body = _load(raw)
    if (not isinstance(body, dict) or not isinstance(body.get("metadata"), dict)
            or body["metadata"].get("identifier") != item):
        raise ValueError("Archive metadata identity mismatch")
    files = body.get("files")
    if not isinstance(files, list) or len(files) > 10000:
        raise ValueError("Invalid archive file listing")
    base = f"https://archive.org/download/{item}/"
    candidates = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("Invalid archive file entry")
        if entry.get("source") != "original":
            continue
        candidates.append(_candidate(entry.get("name"), entry.get("size"), entry.get("sha1"),
                                     "sha1", family=family, extension=extension, base=base))
    return _latest(candidates)


def protomaps_latest(raw, *, version_major=4):
    """Select latest compatible complete Protomaps planetary archive."""
    builds = _load(raw)
    if not isinstance(builds, list) or len(builds) > 10000:
        raise ValueError("Invalid map build listing")
    candidates = []
    for entry in builds:
        if not isinstance(entry, dict):
            raise ValueError("Invalid map build entry")
        version = entry.get("version", "")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            continue
        if int(version.split(".")[0]) != version_major:
            continue
        candidates.append(_candidate(entry.get("key"), entry.get("size"), entry.get("b3sum"),
                                     "blake3", family="", extension="pmtiles",
                                     base="https://build.protomaps.com/", compact=True))
    return _latest(candidates)


def internet_archive_identity(source_url):
    """Return the stable item endpoint and edition family for a dated ZIM URL."""
    url = urlsplit(source_url)
    match = re.fullmatch(r"/download/([a-zA-Z0-9_-]{1,128})/([a-zA-Z0-9_-]{1,120}?)(20\d{2}-\d{2}-\d{2})([a-z]?)\.zim", url.path)
    if url.scheme != "https" or url.netloc != "archive.org" or not match or url.query or url.fragment:
        return None
    item, family, day, revision = match.groups()
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return dict(endpoint=f"https://archive.org/metadata/{item}", family=family, edition=day + revision)


def fetch_metadata(endpoint):
    """Bounded HTTPS JSON transfer using the existing pinned transport."""
    from . import pdf_transport
    with pdf_transport.open_pdf(endpoint, format_name="zim", limit=MAX_METADATA_BYTES,
                                total_seconds=25) as stream:
        blocks = bytearray()
        while block := stream.read(65536):
            blocks.extend(block)
        return bytes(blocks)


def check_archive_update(installed, *, fetch=None):
    unknown = dict(state="unable_to_determine", evidence="Publisher archive metadata could not be verified")
    try:
        identity = internet_archive_identity(installed["source_url"])
        if not identity or installed.get("verified") is not True or not re.fullmatch(r"[a-f0-9]{40}", installed["publisher_sha1"]):
            return unknown
        endpoint = identity["endpoint"]
        latest = internet_archive_latest((fetch or fetch_metadata)(endpoint), endpoint=endpoint, family=identity["family"])
        common = dict(installed_edition=identity["edition"], candidate_edition=latest["edition"])
        if latest["edition"] < identity["edition"]:
            return dict(common, state="older_candidate", evidence="Publisher listed an older edition")
        if latest["edition"] == identity["edition"]:
            if latest["digest"] == installed["publisher_sha1"]:
                return dict(common, state="up_to_date", evidence="Publisher edition and SHA-1 match installed archive")
            return dict(unknown, evidence="Publisher changed the digest of the same dated edition")
        return dict(common, state="update_available", evidence="Publisher listed a newer dated archive with SHA-1", candidate_url=latest["source_url"], candidate_bytes=latest["bytes"], candidate_sha1=latest["digest"])
    except (ValueError, TypeError, KeyError, OSError):
        return unknown
