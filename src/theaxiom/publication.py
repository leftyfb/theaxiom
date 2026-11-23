"""Persistent publication intent and fail-closed serving policy."""
import http.client
import json
import os
import tempfile
import threading
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, quote
from pathlib import Path

from . import catalog_store, storage_settings, source_catalog

LOCK = threading.RLock()
# Compatibility reader metadata comes from immutable migration definitions,
# not from a source identity branch or mutable administrator profile.
def legacy_reader_routes():
    seeds = {entry["source_id"]: entry for entry in source_catalog.builtin_sources()}
    routes = {}
    for sid, config in source_catalog.LEGACY_READER_ROUTES.items():
        seed = seeds.get(sid)
        if (sid not in source_catalog.LEGACY_ACQUISITION_UNITS
                or not seed or seed.get("kind") != "zim"
                or not isinstance(seed.get("filename"), str)
                or not valid_book_id_from_seed(seed)):
            continue
        routes[sid] = {**config, "port": 8081, "reader_path": "/zim/",
                       "book_id": Path(seed["filename"]).stem}
    return routes


def valid_book_id_from_seed(seed):
    filename = seed.get("filename")
    return bool(isinstance(filename, str) and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,195}\.zim", filename)
                and isinstance(seed.get("sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", seed["sha256"])
                and type(seed.get("bytes")) is int and seed["bytes"] >= 96)


ROUTES = legacy_reader_routes()


def managed_installation(source_id):
    """Return whether a source has crossed into the normal ZIM lifecycle.

    Adoption deliberately uses the same durable completed job as a normal
    acquisition. Until that record is present, the legacy service remains
    the only eligible publication path.
    """
    try:
        from . import zim_jobs
        job = zim_jobs.status(source_id)
        # Legacy routes may cross into the managed lifecycle only through the
        # explicit adoption transition.  A completed job by itself is not
        # evidence of that transition.
        adopted = job.get("adopted_from_legacy") if job else None
        return bool(source_id in ROUTES and isinstance(adopted, dict)
                    and job and job.get("state") == "completed"
                    and job.get("verified") is True
                    and job.get("active") is False
                    and zim_jobs.installed(job, job["configuration"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def legacy_only(source_id):
    return source_id in ROUTES and not managed_installation(source_id)


def state_path():
    return storage_settings.SETTINGS_PATH.with_name("publication.json")


def load():
    with LOCK:
        try:
            data = json.loads(state_path().read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or any(not catalog_store.ID_RE.fullmatch(k) or type(v) is not bool
                                             for k, v in data.items()):
            raise ValueError("invalid publication state")
        return data


def save(source_id, enabled):
    if not catalog_store.ID_RE.fullmatch(source_id) or type(enabled) is not bool:
        raise ValueError("invalid publication request")
    with LOCK:
        data = load()
        if enabled or source_id in ROUTES:
            data[source_id] = enabled
        else:
            data.pop(source_id, None)
        target = state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".publication-", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)


def verified(snapshot, source_id):
    return snapshot.get("stale") is False and any(
        job.get("source_id") == source_id and job.get("state") == "completed"
        and job.get("verified") is True and job.get("active") is False
        for job in snapshot.get("acquisitions", []))


def reachable(route):
    connection = http.client.HTTPConnection("127.0.0.1", route["port"], timeout=1)
    try:
        # Publisher-named legacy routes declare a pinned book ID as data.
        # Verify the exact book through the shared Kiwix OPDS endpoint;
        # source IDs and publisher names never select a special code path.
        if route.get("book_id") is not None:
            book_id = route["book_id"]
            if not valid_book_id(book_id):
                return False
            return reader_location(
                {"path": route.get("reader_path", "/zim/"), "port": route["port"]},
                book_id,
            ) is not None
        connection.request("GET", route["path"])
        response = connection.getresponse()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


# Generic Kiwix book IDs are derived from verified digest-suffixed filenames.
# Keep this separate from catalog source IDs, which must never contain dots.
GENERIC_BOOK_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-f0-9]{64}\Z")


# Earlier verified installations may have publisher-named (rather than
# digest-suffixed) ZIMs. Their exact allowlisted names are trusted data, not
# special runtime behavior attached to a publisher or source ID.
def valid_book_id(book_id):
    if not isinstance(book_id, str):
        return False
    if GENERIC_BOOK_ID_RE.fullmatch(book_id):
        return True
    from . import source_catalog
    for entry in source_catalog.builtin_sources():
        if entry.get("kind") != "zim":
            continue
        filename, digest, size = (entry.get("filename"), entry.get("sha256"), entry.get("bytes"))
        if (not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,195}\.zim", filename)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int or size < 96):
            continue
        if book_id == Path(filename).stem:
            return True
    return False


def reader_location(route, book_id=None):
    """Discover the single local book's content link from Kiwix OPDS.

    Only accept a local, prefix-scoped book link. Never follow a remote URL
    or return catalog-supplied executable markup.
    """
    if book_id is not None and not valid_book_id(book_id):
        return None
    connection = http.client.HTTPConnection("127.0.0.1", route["port"], timeout=2)
    try:
        connection.request("GET", route["path"] + ("catalog/v2/entries?count=101" if book_id else "catalog/v2/entries?count=2"))
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = response.read(262145)
        if len(data) > 262144 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            return None
        root = ET.fromstring(data)
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if not entries or len(entries) > (100 if book_id else 1):
            return None
        prefix = route["path"] + "content/"
        names = set()
        for entry in entries:
            entry_names = set()
            for link in entry.findall("{http://www.w3.org/2005/Atom}link"):
                href = link.get("href", "")
                u = urlsplit(href)
                if u.scheme or u.netloc or u.query or u.fragment or not u.path.startswith(prefix):
                    continue
                name = u.path[len(prefix):].rstrip("/")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", name):
                    entry_names.add(name)
            # An ambiguous entry can never authenticate a reader destination.
            if len(entry_names) != 1:
                return None
            name = next(iter(entry_names))
            if book_id is None or name == book_id:
                names.add(name)
        if len(names) != 1:
            return None
        return route["path"] + "viewer#" + quote(names.pop(), safe="") + "/"
    except (OSError, http.client.HTTPException, ET.ParseError, ValueError):
        return None
    finally:
        connection.close()


def status(snapshot, source_id, *, probe=True):
    if source_id in ROUTES and managed_installation(source_id):
        from . import zim_catalog
        try:
            record = next(r for r in snapshot.get("sources", [])
                          if r.get("source_id") == source_id)
            return zim_catalog.status(record, snapshot.get("sources", []))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return {"enabled": False, "verified": False, "serving": False,
                    "published": False, "path": "/read/zim/" + source_id,
                    "entry_path": "/read/zim/" + source_id,
                    "error": "publication state unavailable"}
    route = ROUTES[source_id]
    try:
        enabled = load().get(source_id, False)
        ready = verified(snapshot, source_id)
        serving = ready and probe and reachable(route)
        return {"enabled": enabled, "verified": ready, "serving": serving,
                "published": enabled and serving, "path": route["path"]}
    except (OSError, ValueError):
        return {"enabled": False, "verified": False, "serving": False,
                "published": False, "path": route["path"], "error": "publication state unavailable"}


def allowed(snapshot, source_id):
    if source_id in ROUTES and managed_installation(source_id):
        from . import zim_catalog
        try:
            record = next(r for r in snapshot.get("sources", [])
                          if r.get("source_id") == source_id)
            return bool(zim_catalog.status(record, snapshot.get("sources", []))["published"])
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return False
    try:
        return source_id in ROUTES and load().get(source_id, False) and verified(snapshot, source_id)
    except (OSError, ValueError):
        return False
