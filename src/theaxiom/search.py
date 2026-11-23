"""Offline search coordination. Adapters only receive trusted published routes."""
from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
from typing import Callable
from urllib.parse import urlencode, urlsplit, unquote
import threading
import time
import xml.etree.ElementTree as ET

from . import document_search, publication

MAX_QUERY = 160
MAX_RESULTS = 20
MAX_RSS_BYTES = 512 * 1024
MAX_SEARCH_SECONDS = 20
KIWIX_SEARCH_SECONDS = 8
MAX_PAGE = 50_000
KIWIX_CAPABILITY = "Kiwix full-text search"
_SEARCH_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True)
class Result:
    title: str
    href: str
    snippet: str = ""


@dataclass(frozen=True)
class Page:
    results: list[Result]
    has_more: bool = False
    partial: bool = False
    start_base: int = 1


@dataclass(frozen=True)
class Adapter:
    capability: str
    search: Callable[[dict, str], list[Result] | Page]


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
        prefix = route["path"]
        if route.get("kind") == "pdf":
            return (not parsed.scheme and not parsed.netloc and not parsed.query and parsed.path == prefix
                    and parsed.fragment.startswith("page=") and parsed.fragment[5:].isascii()
                    and parsed.fragment[5:].isdigit())
        if route.get("kind") == "epub":
            suffix = parsed.path.removeprefix(prefix + "/chapter/")
            return (not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment
                    and suffix.isascii() and suffix.isdigit())
        return (not parsed.scheme and not parsed.netloc
                and parsed.path.startswith(route["path"])
                and decoded.startswith(route["path"])
                and not any(c in decoded for c in ("\\", "%"))
                and not any(ord(c) < 32 or ord(c) == 127 for c in href + decoded)
                and not any(p in (".", "..") for p in decoded.split("/")))
    except (ValueError, TypeError):
        return False


def search(query: str, snapshot: Callable[[], dict], *, source_ids=None, targets=None, allowed=None, page=1, start_base=1) -> list[dict]:
    query = validate_query(query)
    if not query:
        return []
    if not _SEARCH_SLOTS.acquire(blocking=False):
        raise RuntimeError("Search is busy; try again")
    try:
        groups = []
        deadline = time.monotonic() + MAX_SEARCH_SECONDS
        if targets is None:
            targets = {sid: (route, ADAPTERS.get(sid)) for sid, route in publication.ROUTES.items()}
        check = allowed or (lambda sid, route: publication.allowed(snapshot(), sid))
        for source_id, (route, adapter) in targets.items():
            if source_ids is not None and source_id not in source_ids:
                continue
            if not check(source_id, route):
                continue
            results = []
            status = "unsupported"
            has_more = False
            result_base = start_base
            if adapter:
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Search time budget used")
                    answer = adapter.search(
                        dict(route, deadline=deadline, offset=(page - 1) * MAX_RESULTS,
                             start_base=start_base), query)
                    answer = answer if isinstance(answer, Page) else Page(answer)
                    results = [Result(r.title[:300], r.href, r.snippet[:600])
                               for r in answer.results[:MAX_RESULTS]
                               if r.title and valid_link(r.href, route)]
                    status = "partial" if answer.partial else "ok"
                    has_more = answer.has_more and page < MAX_PAGE
                    result_base = answer.start_base
                except document_search.IndexPending:
                    status = "indexing"
                except document_search.NoText:
                    status = "no_text"
                except TimeoutError:
                    status = "timeout"
                except (OSError, ValueError, RuntimeError):
                    status = "unavailable"
            groups.append(dict(source_id=source_id, name=route["name"],
                               capability=adapter.capability if adapter else "",
                               status=status, results=results, page=page, has_more=has_more,
                               start_base=result_base))
        return [group for group in groups
                if check(group["source_id"], targets[group["source_id"]][0])]
    finally:
        _SEARCH_SLOTS.release()



class _SnippetText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def parse_kiwix_results(data, route):
    """Read a bounded RSS response without trusting result URLs or markup."""
    if len(data) > MAX_RSS_BYTES or b"\x00" in data or b"<!" in data.upper().replace(b"<![CDATA[", b""):
        raise ValueError("Invalid Kiwix search feed")
    try:
        root = ET.fromstring(data.decode("utf-8"))
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError("Invalid Kiwix search feed") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Invalid Kiwix search feed")
    results = []
    seen = set()
    items = channel.findall("item")
    for item in items[:MAX_RESULTS]:
        title = (item.findtext("title") or "").strip()
        href = item.findtext("link") or ""
        if not title or len(href) > 2048 or not valid_link(href, route) or href in seen:
            continue
        parts = urlsplit(href)
        if parts.query or parts.fragment or parts.path == route["path"]:
            continue
        snippet = _SnippetText()
        snippet.feed("".join(item.find("description").itertext())[:2400]
                     if item.find("description") is not None else "")
        snippet.close()
        results.append(Result(title[:300], href, " ".join(" ".join(snippet.parts).split())[:600]))
        seen.add(href)
    # The first page uses Kiwix's default start so its RSS identifies the
    # indexing convention. Carry it with navigation to avoid another query.
    start_base = route.get("start_base", 1)
    if not route.get("offset"):
        start = channel.findtext("{http://a9.com/-/spec/opensearch/1.1/}startIndex")
        if start in {"0", "1"}:
            start_base = int(start)
    return Page(results, has_more=len(items) > MAX_RESULTS, start_base=start_base)


def search_kiwix(route, query):
    """Query one verified book's existing index on the local Kiwix listener."""
    book = route["book_id"]
    if not publication.valid_book_id(book) or route["path"] != f"/zim/content/{book}/":
        raise ValueError("Invalid Kiwix search target")
    deadline = min(route["deadline"], time.monotonic() + KIWIX_SEARCH_SECONDS)
    params = {"books.name": book, "pattern": query, "format": "xml",
              "pageLength": MAX_RESULTS + 1}
    if route.get("offset"):
        params["start"] = route["offset"] + route.get("start_base", 1)
    connection = http.client.HTTPConnection("127.0.0.1", route["port"], timeout=max(0.001, deadline - time.monotonic()))
    try:
        connection.request("GET", "/zim/search?" + urlencode(params), headers={"Accept": "application/rss+xml"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError("Kiwix search unavailable")
            body = bytearray()
            while len(body) <= MAX_RSS_BYTES:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Kiwix search timed out")
                chunk = response.read1(min(8192, MAX_RSS_BYTES + 1 - len(body)))
                if not chunk:
                    return parse_kiwix_results(bytes(body), route)
                body.extend(chunk)
            raise ValueError("Kiwix search feed exceeds byte limit")
    except http.client.HTTPException as exc:
        raise ValueError("Kiwix search unavailable") from exc
    finally:
        connection.close()


KIWIX_ADAPTER = Adapter(KIWIX_CAPABILITY, search_kiwix)

DOCUMENT_ADAPTER = Adapter("Document text search", document_search.search_document)
