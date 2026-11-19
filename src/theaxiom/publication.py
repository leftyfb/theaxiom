"""Persistent publication intent and fail-closed serving policy."""
import http.client
import json
import os
import tempfile
import threading
from pathlib import Path

from . import storage_settings

LOCK = threading.RLock()
# Trusted deployment configuration, not administrator-supplied URLs.
ROUTES = {"wikipedia-en": {"path": "/wikipedia/", "port": 8081,
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
        if not isinstance(data, dict) or any(k not in ROUTES or type(v) is not bool
                                             for k, v in data.items()):
            raise ValueError("invalid publication state")
        return data


def save(source_id, enabled):
    if source_id not in ROUTES or type(enabled) is not bool:
        raise ValueError("invalid publication request")
    with LOCK:
        data = load()
        data[source_id] = enabled
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
