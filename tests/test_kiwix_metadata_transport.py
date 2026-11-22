"""No-network security and limit fixtures for Kiwix metadata fetch."""
from email.message import Message
from io import BytesIO
import socket

import pytest

from theaxiom import kiwix_metadata_transport as transport

CATALOG = "https://opds.library.kiwix.org/catalog/v2/entries"
META = "https://download.kiwix.org/zim/wikipedia/example_2026-11.zim.meta4"


class Response:
    def __init__(self, status=200, headers=(), body=b"<feed/>"):
        self.status = status
        self.headers = Message()
        for key, value in headers:
            self.headers[key] = value
        self.body = BytesIO(body)

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.body.close()


def fake_transport(monkeypatch, responses, *, expected_host):
    calls = []
    monkeypatch.setattr(transport.pdf_transport, "_public_addresses", lambda host: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (host, 443))
    ])
    class Connection:
        def __init__(self, host, address, deadline):
            assert host == expected_host
            assert address[-1] == (host, 443)
        def request(self, method, path, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return responses[0]
        def close(self):
            calls.append(("closed",))
    monkeypatch.setattr(transport.pdf_transport, "_PinnedHTTPS", Connection)
    return calls


def test_catalog_fetch_is_bounded_and_has_no_side_effects(monkeypatch):
    response = Response(headers=[("Content-Length", "6")], body=b"123456")
    calls = fake_transport(monkeypatch, [response], expected_host="opds.library.kiwix.org")
    assert transport.fetch_metadata(CATALOG, kind="catalog") == b"123456"
    assert calls[0][0:2] == ("GET", "/catalog/v2/entries")
    assert calls[0][2]["Accept-Encoding"] == "identity"
    assert response.body.closed
    assert calls[-1] == ("closed",)


def test_metalink_fetch_uses_exact_path(monkeypatch):
    response = Response(body=b"<metalink/>")
    calls = fake_transport(monkeypatch, [response], expected_host="download.kiwix.org")
    assert transport.fetch_metadata(META, kind="metalink") == b"<metalink/>"
    assert calls[0][1] == "/zim/wikipedia/example_2026-11.zim.meta4"


@pytest.mark.parametrize("url,kind", [
    ("http://opds.library.kiwix.org/catalog/v2/entries", "catalog"),
    ("https://localhost/catalog/v2/entries", "catalog"),
    ("https://opds.library.kiwix.org/catalog/v2/entries?url=other", "catalog"),
    ("https://opds.library.kiwix.org/catalog/v2/entries/other", "catalog"),
    ("https://user@opds.library.kiwix.org/catalog/v2/entries", "catalog"),
    ("https://opds.library.kiwix.org:444/catalog/v2/entries", "catalog"),
    ("https://evil.invalid/zim/a.zim.meta4", "metalink"),
    ("https://download.kiwix.org/zim/../secret.zim.meta4", "metalink"),
    ("https://download.kiwix.org/zim/a.zim", "metalink"),
    ("https://download.kiwix.org/zim/a.zim.meta4?x=1", "metalink"),
    (CATALOG, "invalid"),
])
def test_invalid_endpoint_fails_before_dns(monkeypatch, url, kind):
    monkeypatch.setattr(transport.pdf_transport, "_public_addresses",
                        lambda *args: pytest.fail("unsafe request tried DNS"))
    with pytest.raises(ValueError):
        transport.fetch_metadata(url, kind=kind)


@pytest.mark.parametrize("response", [
    Response(status=302, headers=[("Location", "https://other.invalid/test")]),
    Response(status=304),
    Response(status=429),
    Response(status=200, headers=[("Content-Length", "99999999")]),
    Response(status=200, headers=[("Content-Length", "3"), ("Content-Length", "4")]),
    Response(status=200, headers=[("Content-Length", "5")], body=b"abc"),
    Response(status=200, headers=[("Content-Encoding", "gzip")]),
    Response(status=200, headers=[("Transfer-Encoding", "gzip")]),
    Response(status=200, headers=[("Transfer-Encoding", "chunked"), ("Content-Length", "7")]),
    Response(status=200, headers=[("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "gzip")]),
])
def test_invalid_response_fails_closed(monkeypatch, response):
    fake_transport(monkeypatch, [response], expected_host="opds.library.kiwix.org")
    with pytest.raises(ValueError):
        transport.fetch_metadata(CATALOG, kind="catalog")
    assert response.body.closed


def test_standard_chunked_metadata_is_decoded_and_bounded(monkeypatch):
    response = Response(headers=[("Transfer-Encoding", "chunked")], body=b"<feed/>")
    fake_transport(monkeypatch, [response], expected_host="opds.library.kiwix.org")
    assert transport.fetch_metadata(CATALOG, kind="catalog") == b"<feed/>"
    assert response.body.closed

    oversized = Response(headers=[("Transfer-Encoding", "chunked")],
                         body=b"x" * (transport.kiwix_opds.MAX_BYTES + 1))
    fake_transport(monkeypatch, [oversized], expected_host="opds.library.kiwix.org")
    with pytest.raises(ValueError, match="byte cap"):
        transport.fetch_metadata(CATALOG, kind="catalog")


def test_streamed_body_limit_without_content_length(monkeypatch):
    response = Response(body=b"a" * (transport.kiwix_opds.MAX_METALINK_BYTES + 1))
    fake_transport(monkeypatch, [response], expected_host="download.kiwix.org")
    with pytest.raises(ValueError, match="byte cap"):
        transport.fetch_metadata(META, kind="metalink")
    assert response.body.closed


def test_nonpublic_dns_rejected_before_tls(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
    ])
    monkeypatch.setattr(transport.pdf_transport, "_PinnedHTTPS",
                        lambda *args: pytest.fail("TLS must not run"))
    with pytest.raises(ValueError, match="non-public"):
        transport.fetch_metadata(CATALOG, kind="catalog")


def test_bounded_catalog_query_reaches_exact_encoded_request_path(monkeypatch):
    query = transport.catalog_page_url(
        family="wikipedia_en_all_maxi", language="eng", start=25, count=25)
    response = Response(body=b"<feed/>")
    calls = fake_transport(monkeypatch, [response], expected_host="opds.library.kiwix.org")
    assert transport.fetch_metadata(query, kind="catalog") == b"<feed/>"
    assert calls[0][1] == (
        "/catalog/v2/entries?name=wikipedia_en_all_maxi&lang=eng&start=25&count=25"
    )


@pytest.mark.parametrize("kwargs", [
    {"family": "../etc"}, {"family": "a"},
    {"family": "wikipedia_en_all_maxi", "language": "en"},
    {"family": "wikipedia_en_all_maxi", "start": -1},
    {"family": "wikipedia_en_all_maxi", "start": 1001},
    {"family": "wikipedia_en_all_maxi", "start": True},
    {"family": "wikipedia_en_all_maxi", "count": 0},
    {"family": "wikipedia_en_all_maxi", "count": 51},
    {"family": "wikipedia_en_all_maxi", "count": True},
])
def test_catalog_url_builder_refuses_unbounded_or_ambiguous_filters(kwargs):
    with pytest.raises(ValueError):
        transport.catalog_page_url(**dict(
            family="wikipedia_en_all_maxi", language="eng", **{
                k: v for k, v in kwargs.items()
                if k not in {"family", "language"}
            }) if "family" not in kwargs and "language" not in kwargs
            else dict({"family": "wikipedia_en_all_maxi", "language": "eng"}, **kwargs))


@pytest.mark.parametrize("url", [
    CATALOG + "?count=-1",
    CATALOG + "?count=0",
    CATALOG + "?name=wikipedia_en_all_maxi&lang=eng&start=0&count=0",
    CATALOG + "?name=wikipedia_en_all_maxi&lang=eng&start=0&count=51",
    CATALOG + "?name=wikipedia_en_all_maxi&lang=eng&start=0&count=25&count=25",
    CATALOG + "?name=wikipedia_en_all_maxi&lang=eng&start=0&count=25&url=http://internal/",
    CATALOG + "?name=wikipedia_en_all_maxi&lang=eng&start=00&count=25",
    CATALOG + "?lang=eng&name=wikipedia_en_all_maxi&start=0&count=25",
])
def test_catalog_refuses_unbounded_duplicate_or_noncanonical_queries(monkeypatch, url):
    monkeypatch.setattr(transport.pdf_transport, "_public_addresses",
                        lambda *args: pytest.fail("unsafe query tried DNS"))
    with pytest.raises(ValueError):
        transport.fetch_metadata(url, kind="catalog")
