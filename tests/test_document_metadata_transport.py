"""Offline HEAD-only metadata security/validator regression tests."""
from email.message import Message
import socket

import pytest

from theaxiom import pdf_transport


class HeadResponse:
    def __init__(self, code=200, headers=()):
        self.status = code
        self.headers = Message()
        for name, value in headers:
            self.headers[name] = value
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, *args):
        pytest.fail("HEAD must never read a body")

    def read1(self, *args):
        pytest.fail("HEAD must never read a body")

    def close(self):
        self.closed = True


def mock_head(monkeypatch, response):
    calls = []
    monkeypatch.setattr(pdf_transport, "_public_addresses", lambda host: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (host, 443)),
    ])
    class Connection:
        def __init__(self, host, address, deadline):
            assert address[-1] == (host, 443)
        def request(self, method, path, headers):
            calls.append((method, path, headers))
            assert method == "HEAD"
            assert path == "/guide.pdf"
        def getresponse(self):
            return response
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)
    return calls


def test_strong_etag_metadata_is_returned_without_reading_content(monkeypatch):
    response = HeadResponse(200, [
        ("ETag", '"release-2"'), ("Content-Length", "1234"),
        ("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT"),
    ])
    calls = mock_head(monkeypatch, response)
    result = pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf",
                                                    if_none_match='"release-1"')
    assert result["reachable"] and result["etag"] == '"release-2"'
    assert result["content_length"] == "1234"
    assert calls[0][2]["If-None-Match"] == '"release-1"'
    assert response.closed and calls[-1] == ("close",)


def test_conditional_304_is_metadata_only(monkeypatch):
    response = HeadResponse(304, [("ETag", '"release-1"')])
    mock_head(monkeypatch, response)
    result = pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf",
                                                    if_none_match='"release-1"')
    assert result["status"] == 304
    assert result["reachable"]


@pytest.mark.parametrize("code", [301, 302, 307, 308, 403, 405, 429, 500])
def test_non_200_304_does_not_follow_redirect_or_claim_success(monkeypatch, code):
    response = HeadResponse(code, [("Location", "https://evil.invalid/secret")])
    calls = mock_head(monkeypatch, response)
    result = pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf")
    assert not result["reachable"] and result["error"] == "metadata unavailable"
    assert len([call for call in calls if call[0] == "HEAD"]) == 1
    assert response.closed


@pytest.mark.parametrize("headers", [
    [("ETag", '"v1"'), ("ETag", '"v2"')],
    [("Content-Length", "2"), ("Content-Length", "3")],
    [("Content-Length", "-1")],
    [("Content-Length", "12, 14")],
    [("Content-Encoding", "gzip")],
    [("Transfer-Encoding", "chunked")],
])
def test_ambiguous_metadata_fails_closed(monkeypatch, headers):
    response = HeadResponse(200, headers)
    mock_head(monkeypatch, response)
    with pytest.raises(ValueError):
        pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf")
    assert response.closed


def test_weak_etag_is_not_trusted_as_validator(monkeypatch):
    response = HeadResponse(200, [("ETag", 'W/"weak"'), ("Content-Length", "333")])
    mock_head(monkeypatch, response)
    result = pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf")
    assert result["etag"] == "" and result["content_length"] == "333"


@pytest.mark.parametrize("validator", ['W/"weak"', "bad", '"line\r\n"'])
def test_unsafe_validator_rejected_before_dns(monkeypatch, validator):
    monkeypatch.setattr(pdf_transport, "_public_addresses",
                        lambda host: pytest.fail("Invalid validator reached DNS"))
    with pytest.raises(ValueError):
        pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf",
                                              if_none_match=validator)


@pytest.mark.parametrize("url", [
    "http://publisher.org/guide.pdf",
    "https://127.0.0.1/guide.pdf",
    "https://user@publisher.org/guide.pdf",
    "https://publisher.org:444/guide.pdf",
    "https://publisher.org/guide.pdf?token=secret",
])
def test_unsafe_url_is_rejected_without_network(monkeypatch, url):
    monkeypatch.setattr(pdf_transport, "_public_addresses",
                        lambda host: pytest.fail("Invalid URL reached DNS"))
    with pytest.raises(ValueError):
        pdf_transport.probe_document_metadata(url)


def test_private_dns_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
    ])
    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS",
                        lambda *args: pytest.fail("Private DNS reached TLS"))
    with pytest.raises(ValueError, match="non-public"):
        pdf_transport.probe_document_metadata("https://publisher.org/guide.pdf")
