"""Offline search coordination. Adapters only receive trusted published routes."""
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, unquote
import threading

from . import publication

MAX_QUERY = 160
MAX_RESULTS = 20
_SEARCH_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True)
class Result:
    title: str
    href: str
    snippet: str = ""


@dataclass(frozen=True)
class Adapter:
    capability: str
    search: Callable[[dict, str], list[Result]]


ADAPTERS: dict[str, Adapter] = {}


def validate_query(query: str) -> str:
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        raise ValueError("Search query too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise ValueError("Invalid search query")
    return query.strip()


def valid_link(href: str, route: dict) -> bool:
    try:
        parsed = urlsplit(href)
        decoded = unquote(parsed.path)
        return (not parsed.scheme and not parsed.netloc
                and parsed.path.startswith(route["path"])
                and decoded.startswith(route["path"])
                and not any(c in decoded for c in ("\\", "%"))
                and not any(ord(c) < 32 or ord(c) == 127 for c in href + decoded)
                and not any(p in (".", "..") for p in decoded.split("/")))
    except (ValueError, TypeError):
        return False


def search(query: str, snapshot: Callable[[], dict]) -> list[dict]:
    query = validate_query(query)
    if not query:
        return []
    if not _SEARCH_SLOTS.acquire(blocking=False):
        raise RuntimeError("Search is busy; try again")
    try:
        groups = []
        for source_id, route in publication.ROUTES.items():
            if not publication.allowed(snapshot(), source_id):
                continue
            adapter = ADAPTERS.get(source_id)
            results = []
            status = "unsupported"
            if adapter:
                try:
                    candidates = adapter.search(route, query)
                    results = [Result(r.title[:300], r.href, r.snippet[:600])
                               for r in candidates[:MAX_RESULTS]
                               if r.title and valid_link(r.href, route)]
                    status = "ok"
                except (OSError, ValueError, RuntimeError):
                    status = "unavailable"
            if publication.allowed(snapshot(), source_id):
                groups.append(dict(source_id=source_id, name=route["name"],
                                   capability=adapter.capability if adapter else "",
                                   status=status, results=results))
        return groups
    finally:
        _SEARCH_SLOTS.release()
