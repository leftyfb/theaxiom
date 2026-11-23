"""Manual, metadata-only Kiwix catalog browsing and offline fallback."""
import http.client
import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import kiwix_metadata_transport, kiwix_opds, storage_settings, zim_source

PAGE_SIZE = 25
MAX_HEADER = 512
CACHE_SLOTS = 8
STALE_AFTER = 48 * 60 * 60
_LOCK = threading.Lock()
_next_refresh = 0.0
_next_inspect = 0.0


def _path():
    return storage_settings.SETTINGS_PATH.with_name("upstream-kiwix-catalog")


def _matching(raw, family):
    pattern = re.compile(re.escape(family) + r"(_(?:mini|maxi|nopic))?_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim(?:\.meta4)?")
    for entry in kiwix_opds.parse_acquisition_feed(raw, include_catalog_metadata=True):
        name = urlsplit(entry["acquisition_url"]).path.rsplit("/", 1)[-1]
        match = pattern.fullmatch(name)
        if match:
            variant, edition = match.groups()
            yield (edition, (variant or "").lstrip("_")), entry


def _entries(raw, family):
    choices = {}
    for key, entry in _matching(raw, family):
        option = choices.setdefault(key, {"title": entry["title"], "edition": key[0], "variant": key[1],
                                          "advertised_size": None, "rights": entry.get("rights"),
                                          "publisher_checksum_listed": False})
        # Prefer explicit archive length when the same listing has a direct and a Metalink link.
        if entry.get("advertised_size") and not option["advertised_size"]:
            option["advertised_size"] = entry["advertised_size"]
        if entry["acquisition_kind"] == "metalink":
            option["publisher_checksum_listed"] = True
    return [choices[key] for key in sorted(choices, reverse=True)][:PAGE_SIZE]


def _slots():
    legacy = _path()
    return tuple(legacy.with_name(f"{legacy.name}.{number}") for number in range(CACHE_SLOTS))


def _save(raw, header):
    targets = [(slot, _read_slot(slot)) for slot in _slots()]
    query = header["family"], header["language"]
    matching = next((slot for slot, value in targets
                     if value and (value["family"], value["language"]) == query), None)
    vacant = next((slot for slot, value in targets if value is None), None)
    target = matching or vacant or min(targets, key=lambda item: item[1]["checked_at"])[0]
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".upstream-kiwix-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(json.dumps(header).encode("ascii") + b"\n" + raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_slot(path, query=None):
    """Read the header alone, or the complete page for an exact query."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1 or
                    info.st_size > kiwix_opds.MAX_BYTES + MAX_HEADER):
                return None
            header = json.loads(file.readline(MAX_HEADER))
            recorded = header["checked_at"]
            if (header["version"] != 1 or not isinstance(header["family"], str)
                    or not isinstance(header["language"], str)
                    or type(recorded) not in (int, float) or not 0 < recorded <= time.time()):
                return None
            if query is None:
                return header
            if (header["family"], header["language"]) == query:
                body = file.read(kiwix_opds.MAX_BYTES + 1)
                if 0 < len(body) <= kiwix_opds.MAX_BYTES:
                    return body, recorded
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_cache(family, language):
    query = family, language
    matches = []
    for path in (*_slots(), _path()):
        header = _read_slot(path)
        if header and (header["family"], header["language"]) == query:
            matches.append((header["checked_at"], path))
    for _, path in sorted(matches, key=lambda item: item[0], reverse=True):
        record = _read_slot(path, query)
        if record:
            return record
    return None


def refresh(family, language):
    """Return only a bounded metadata page and an exact-query cached fallback."""
    global _next_refresh
    url = kiwix_metadata_transport.catalog_page_url(family=family, language=language, count=PAGE_SIZE)
    if _LOCK.acquire(blocking=False):
        try:
            if time.monotonic() >= _next_refresh:
                _next_refresh = time.monotonic() + kiwix_metadata_transport.MAX_SECONDS
                raw = kiwix_metadata_transport.fetch_metadata(url, kind="catalog")
                entries = _entries(raw, family)
                stamp = time.time()
                result = {"entries": entries, "checked_at": stamp, "cached": False, "stale": False}
                try:
                    _save(raw, {"version": 1, "family": family, "language": language, "checked_at": stamp})
                except OSError:
                    result["cache_unavailable"] = True
                return result
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            _LOCK.release()
    cached = _read_cache(family, language)
    if cached:
        raw, stamp = cached
        try:
            return {"entries": _entries(raw, family), "checked_at": stamp, "cached": True,
                    "stale": time.time() - stamp > STALE_AFTER}
        except ValueError:
            pass
    return {"entries": [], "unavailable": True}


def _source_draft(details, source_url, listings, edition, language):
    titles = {" ".join(entry["title"].split()) for entry in listings}
    if not source_url or len(source_url) > 512 or len(titles) != 1:
        return None
    rights = {" ".join(entry.get("rights", "").split()) for entry in listings}
    fields = {
        "title": titles.pop(), "format": "zim", "source_url": source_url,
        "edition": edition, "language": language,
        "expected_size_bytes": details["bytes"], "sha256": details["sha256"],
        "license": rights.pop() if len(rights) == 1 else "",
        "provenance": "Kiwix OPDS and Metalink publisher metadata",
    }
    try:
        zim_source.candidate(fields)
    except ValueError:
        return None
    return fields


def inspect_edition(family, language, edition, variant):
    """Resolve publisher metadata for one explicit edition from the latest cached listing."""
    global _next_inspect
    kiwix_metadata_transport.catalog_page_url(family=family, language=language)
    if (not isinstance(edition, str) or not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", edition)
            or variant not in ("", "mini", "maxi", "nopic")):
        raise ValueError("Invalid publisher edition or variant")
    cached = _read_cache(family, language)
    if not cached or time.time() - cached[1] > STALE_AFTER:
        raise ValueError("Refresh this catalog query before inspecting an edition")
    raw, _stamp = cached
    listings = [entry for key, entry in _matching(raw, family)
                if key == (edition, variant) and entry["acquisition_kind"] == "metalink"]
    links = {entry["acquisition_url"] for entry in listings}
    if len(links) != 1:
        raise ValueError("No unique publisher checksum for this edition")
    if not _LOCK.acquire(blocking=False):
        return {"unavailable": True}
    try:
        if time.monotonic() < _next_inspect:
            return {"unavailable": True}
        _next_inspect = time.monotonic() + kiwix_metadata_transport.MAX_SECONDS
        url = links.pop()
        parsed = urlsplit(url)
        name = parsed.path.rsplit("/", 1)[-1].removesuffix(".meta4")
        raw = kiwix_metadata_transport.fetch_metadata(url, kind="metalink-inspection")
        # The URL can only be used if the Metalink explicitly lists this
        # exact publisher URL. Never send an archive request during inspection.
        details = kiwix_opds.inspect_metalink(raw, expected_filename=name,
                                            publisher_url=url.removesuffix(".meta4"))
        draft = _source_draft(details, details.pop("source_url", None), listings, edition, language)
        if draft:
            details["draft"] = draft
        return details
    except (OSError, ValueError, http.client.HTTPException):
        return {"unavailable": True}
    finally:
        _LOCK.release()
