"""Resolve pre-ledger Wikipedia reader URLs against the live serving record."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit


def _book_id(record, route, *, managed, zim_catalog):
    if managed:
        return zim_catalog.source_content_name(record)
    return route.get("book_id")


def resolve_wikipedia(path, query, *, records, route, published, managed, zim_catalog):
    """Return a publication-gated compatibility Location, or ``None``.

    The managed path gets its content ID from the installed artifact, so a
    replacement changes the alias without changing nginx configuration. The
    legacy path retains its immutable pre-ledger book ID until adoption.
    """
    if not published or not isinstance(path, str) or not path.startswith("/wikipedia/"):
        return None
    parsed = urlsplit(path)
    suffix = parsed.path.removeprefix("/wikipedia/")
    book_id = _book_id(records, route, managed=managed, zim_catalog=zim_catalog)
    if not book_id or not isinstance(book_id, str):
        return None
    if suffix == "search":
        params = [("books.name", book_id)]
        if query:
            params.append(("", query))
        # The caller supplies the original query string verbatim so native
        # Kiwix parameters, repeated keys, and encoding are not normalized.
        target = "/zim/search?books.name=" + quote(book_id, safe="")
        if query:
            target += "&" + query
        return target
    if suffix.startswith("content/"):
        article = suffix.removeprefix("content/")
        if not article:
            return None
        return "/zim/content/" + quote(book_id, safe="") + "/" + article + (("?" + query) if query else "")
    if suffix == "viewer":
        return "/zim/viewer" + (("?" + query) if query else "")
    return "/zim/" + suffix + (("?" + query) if query else "")
