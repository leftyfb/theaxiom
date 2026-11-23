"""Resolve pre-ledger Wikipedia reader URLs against the live serving record."""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit


def _book_id(record, route, *, managed, zim_catalog):
    if managed:
        return zim_catalog.source_content_name(record)
    return route.get("book_id")


def translate_viewer_fragment(fragment, legacy_book, managed_book):
    """Translate a Kiwix viewer hash whose first component is a book ID."""
    prefix = "#" + legacy_book
    if fragment == prefix or fragment.startswith(prefix + "/"):
        return "#" + managed_book + fragment[len(prefix):]
    return fragment


def viewer_redirect_document(legacy_book, managed_book, *, nonce):
    """Return a same-origin shim that can see and rewrite ``location.hash``.

    HTTP never receives URL fragments, so a 302 cannot translate old Kiwix
    hashes. The shim performs the translation before entering the managed
    viewer and keeps unrelated hashes intact.
    """
    script = (
        "const oldBook=" + json.dumps(legacy_book) + ";"
        "const newBook=" + json.dumps(managed_book) + ";"
        "const oldHash=location.hash;"
        "const prefix='#'+oldBook;"
        "const hash=(oldHash===prefix||oldHash.startsWith(prefix+'/'))"
        "?'#'+newBook+oldHash.slice(prefix.length):oldHash;"
        "location.replace('/zim/viewer'+location.search+hash);"
    )
    return ("<!doctype html><meta charset='utf-8'><title>Redirecting</title>"
            '<script nonce="' + nonce + '">' + script.replace("<", "\\u003c") + "</script>")


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
        # Kiwix treats repeated books.name selectors ambiguously. Replace any
        # selector supplied by an old bookmark with the canonical book.
        params = [(key, value) for key, value in parse_qsl(
            query, keep_blank_values=True) if key != "books.name"]
        return "/zim/search?" + urlencode(
            [("books.name", book_id), *params], doseq=True)
    if suffix.startswith("content/"):
        article = suffix.removeprefix("content/")
        if not article:
            return None
        # Older Kiwix links sometimes include the book ID after /content/.
        # The destination book is selected by the publication record, not by
        # that stale path component.
        parts = article.split("/", 1)
        legacy_book = route.get("book_id") if route else None
        if parts[0] in {legacy_book, book_id} and len(parts) == 2:
            article = parts[1]
        return "/zim/content/" + quote(book_id, safe="") + "/" + article + (("?" + query) if query else "")
    if suffix == "viewer":
        return "/zim/viewer" + (("?" + query) if query else "")
    return None
