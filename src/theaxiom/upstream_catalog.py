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

from . import kiwix_metadata_transport, kiwix_opds, storage_settings

PAGE_SIZE = 25
MAX_HEADER = 512
STALE_AFTER = 48 * 60 * 60
_LOCK = threading.Lock()
_next_refresh = 0.0


def _path():
    return storage_settings.SETTINGS_PATH.with_name("upstream-kiwix-catalog")


def _entries(raw, family):
    pattern = re.compile(re.escape(family) + r"(_(?:mini|maxi|nopic))?_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim(?:\.meta4)?")
    choices = {}
    for entry in kiwix_opds.parse_acquisition_feed(raw, include_catalog_metadata=True):
        name = urlsplit(entry["acquisition_url"]).path.rsplit("/", 1)[-1]
        match = pattern.fullmatch(name)
        if not match:
            continue
        variant, edition = match.groups()
        option = {"title": entry["title"], "edition": edition, "variant": (variant or "").lstrip("_"),
                  "advertised_size": entry.get("advertised_size"), "rights": entry.get("rights")}
        key = (edition, option["variant"])
        # Prefer explicit archive length when the same listing has a direct and a Metalink link.
        if key not in choices or (option["advertised_size"] and not choices[key]["advertised_size"]):
            choices[key] = option
    return [choices[key] for key in sorted(choices, reverse=True)][:PAGE_SIZE]


def _save(raw, header):
    target = _path()
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


def _cached(family, language):
    try:
        fd = os.open(_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1 or
                    info.st_size > kiwix_opds.MAX_BYTES + MAX_HEADER):
                return None
            header = json.loads(file.readline(MAX_HEADER))
            recorded = header["checked_at"]
            if (header["version"] != 1 or header["family"] != family or header["language"] != language or
                    type(recorded) not in (int, float) or not 0 < recorded <= time.time()):
                return None
            entries = _entries(file.read(kiwix_opds.MAX_BYTES + 1), family)
            return {"entries": entries, "checked_at": recorded, "cached": True,
                    "stale": time.time() - recorded > STALE_AFTER}
    except (OSError, ValueError, KeyError, TypeError):
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
    return _cached(family, language) or {"entries": [], "unavailable": True}
