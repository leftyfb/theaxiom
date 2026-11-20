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

from . import catalog_store, storage_settings

LOCK = threading.RLock()
# Trusted deployment configuration, not administrator-supplied URLs.
ROUTES = {"wikipedia-en": {"path": "/wikipedia/", "port": 8081, "entry_path": "/read/wikipedia-en",
    "name": "Wikipedia", "category": "Encyclopedia",
    "description": "Explore millions of articles, illustrations and reference material without an internet connection.",
    "symbol": "W", "language": "English"}}


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
        connection.request("GET", route["path"])
        response = connection.getresponse()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def reader_location(route):
    """Discover the single local book's content link from Kiwix OPDS.

    Only accept a local, prefix-scoped book link. Never follow a remote URL
    or return catalog-supplied executable markup.
    """
    connection = http.client.HTTPConnection("127.0.0.1", route["port"], timeout=2)
    try:
        connection.request("GET", route["path"] + "catalog/v2/entries?count=2")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = response.read(262145)
        if len(data) > 262144 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            return None
        root = ET.fromstring(data)
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if len(entries) != 1:
            return None
        prefix = route["path"] + "content/"
        names = set()
        for link in entries[0].findall("{http://www.w3.org/2005/Atom}link"):
            href = link.get("href", "")
            u = urlsplit(href)
            if u.scheme or u.netloc or u.query or u.fragment or not u.path.startswith(prefix):
                continue
            name = u.path[len(prefix):].rstrip("/")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", name):
                names.add(name)
        if len(names) != 1:
            return None
        return route["path"] + "viewer#" + quote(names.pop(), safe="") + "/"
    except (OSError, http.client.HTTPException, ET.ParseError, ValueError):
        return None
    finally:
        connection.close()


def status(snapshot, source_id, *, probe=True):
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
    try:
        return source_id in ROUTES and load().get(source_id, False) and verified(snapshot, source_id)
    except (OSError, ValueError):
        return False
