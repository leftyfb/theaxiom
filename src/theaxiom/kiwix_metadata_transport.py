"""Read-only, bounded HTTPS transport for official Kiwix catalog metadata.

This module never writes content or invokes acquisition. It reuses the
application's pinned-address TLS and public-DNS validation. Redirects are
rejected rather than trusted across hosts; all endpoints are allowlisted.
"""
from __future__ import annotations

import http.client
import re
import time
from urllib.parse import urlsplit, urlencode, parse_qsl

from . import kiwix_opds, pdf_transport

CATALOG_HOST = "opds.library.kiwix.org"
CATALOG_PATH = "/catalog/v2/entries"
MAX_SECONDS = 15
CATALOG_PAGE_LIMIT = 50
MAX_CATALOG_START = 1000


def catalog_page_url(*, family: str, language: str, start: int = 0, count: int = 25) -> str:
    """Construct a restricted publisher-side catalog query, not a generic URL."""
    if (not isinstance(family, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,119}", family)
            or ".." in family or not isinstance(language, str)
            or not re.fullmatch(r"[a-z]{3}", language)
            or type(start) is not int or not 0 <= start <= MAX_CATALOG_START
            or type(count) is not int or not 1 <= count <= CATALOG_PAGE_LIMIT):
        raise ValueError("Invalid bounded Kiwix catalog query")
    return ("https://" + CATALOG_HOST + CATALOG_PATH + "?" + urlencode({
        "name": family, "lang": language, "start": start, "count": count,
    }))


def _validated_endpoint(url: str, kind: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or len(url) > 2048 or kind not in {"catalog", "metalink"}:
        raise ValueError("Invalid Kiwix metadata endpoint")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError("Invalid Kiwix metadata URL")
    except ValueError as exc:
        raise ValueError("Invalid Kiwix metadata URL") from exc
    host = parsed.hostname.lower()
    if kind == "catalog":
        # Filtering and pagination will be handled by the resolver, not
        # arbitrary user query parameters supplied here.
        if host != CATALOG_HOST or parsed.path != CATALOG_PATH:
            raise ValueError("Unapproved Kiwix catalog endpoint")
        if parsed.query:
            try:
                parts = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
                fields = dict(parts)
                if (len(parts) != 4 or set(fields) != {"name", "lang", "start", "count"}
                        or len(fields) != len(parts)
                        or catalog_page_url(
                            family=fields["name"], language=fields["lang"],
                            start=int(fields["start"]), count=int(fields["count"]),
                        ).split("?", 1)[1] != parsed.query):
                    raise ValueError("Unapproved Kiwix catalog filters")
            except (ValueError, OverflowError) as exc:
                raise ValueError("Unapproved Kiwix catalog filters") from exc
        return host, parsed.path + ("?" + parsed.query if parsed.query else ""), kiwix_opds.MAX_BYTES
    if (host not in kiwix_opds.DOWNLOAD_HOSTS or parsed.query
            or not re.fullmatch(r"/zim/[a-zA-Z0-9._/-]+\.zim\.meta4", parsed.path)
            or "/../" in parsed.path or "/./" in parsed.path or "//" in parsed.path):
        raise ValueError("Unapproved Kiwix Metalink endpoint")
    return host, parsed.path, kiwix_opds.MAX_METALINK_BYTES


def fetch_metadata(url: str, *, kind: str) -> bytes:
    """Retrieve only an allowlisted metadata document, with a fixed byte cap.

    No redirects, cookies, auth, or compression. The pinned TLS connection
    verifies the original hostname and does not use environment proxy settings.
    """
    host, path, cap = _validated_endpoint(url, kind)
    deadline = time.monotonic() + MAX_SECONDS
    addresses = pdf_transport._public_addresses(host)
    pdf_transport._remaining(deadline)
    conn = pdf_transport._PinnedHTTPS(host, addresses[0], deadline)
    try:
        conn.request("GET", path, headers={
            "Accept": ("application/atom+xml" if kind == "catalog"
                       else "application/metalink4+xml"),
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "The-Axiom Kiwix metadata checker",
        })
        response = conn.getresponse()
        try:
            pdf_transport._remaining(deadline)
            if response.status != 200:
                raise ValueError(f"Kiwix metadata request rejected (HTTP {response.status})")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise ValueError("Encoded Kiwix metadata response refused")
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if transfers or len(lengths) > 1:
                raise ValueError("Ambiguous Kiwix metadata framing")
            expected = None
            if lengths:
                if not lengths[0].isascii() or not lengths[0].isdecimal():
                    raise ValueError("Invalid Kiwix metadata content length")
                expected = int(lengths[0])
                if expected == 0 or expected > cap:
                    raise ValueError("Kiwix metadata exceeds byte cap")
            body = bytearray()
            while True:
                pdf_transport._remaining(deadline)
                chunk = response.read1(min(8192, cap + 1 - len(body)))
                pdf_transport._remaining(deadline)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > cap:
                    raise ValueError("Kiwix metadata exceeds byte cap")
            if not body or (expected is not None and len(body) != expected):
                raise ValueError("Incomplete Kiwix metadata response")
            return bytes(body)
        finally:
            response.close()
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ValueError("Kiwix metadata connection failed or timed out") from exc
    finally:
        conn.close()
