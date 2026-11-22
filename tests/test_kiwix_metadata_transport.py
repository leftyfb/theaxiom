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
    Response(status=200, headers=[("Transfer-Encoding", "chunked")]),
])
def test_invalid_response_fails_closed(monkeypatch, response):
    fake_transport(monkeypatch, [response], expected_host="opds.library.kiwix.org")
    with pytest.raises(ValueError):
        transport.fetch_metadata(CATALOG, kind="catalog")
    assert response.body.closed


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
