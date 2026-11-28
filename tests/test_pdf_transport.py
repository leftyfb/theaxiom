"""Offline tests for SSRF-resistant PDF transport."""
from email.message import Message
from io import BytesIO
import socket
import ssl

import pytest

from theaxiom import pdf_transport


def addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))
            for ip in ips]


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.0.10", "169.254.169.254",
                                "10.0.0.1", "100.64.0.1", "224.0.0.1", "0.0.0.0"])
def test_reject_nonpublic_or_mixed_dns(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8", ip))
    with pytest.raises(ValueError, match="non-public"):
        pdf_transport._public_addresses("publisher.org")


def test_public_dns_addresses_pinned(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))
    assert pdf_transport._public_addresses("publisher.org")[0][-1] == ("8.8.8.8", 443)


@pytest.mark.parametrize("url", ["http://publisher.org/a.pdf", "https://127.0.0.1/a.pdf",
                                  "https://user@publisher.org/a.pdf", "https://publisher.org:444/a.pdf"])
def test_unsafe_url_rejected_before_dns(monkeypatch, url):
    def unexpected(*a, **k):
        pytest.fail("DNS must not run for invalid URL")
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    with pytest.raises(ValueError):
        with pdf_transport.open_pdf(url):
            pass


class Response:
    def __init__(self, status, headers, body):
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


def mock_response(monkeypatch, status, headers, body=b"abc"):
    response = Response(status, headers, body)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))

    class Connection:
        def __init__(self, host, address, deadline):
            assert host == "publisher.org"
            assert address[-1] == ("8.8.8.8", 443)

        def request(self, method, path, headers):
            assert method == "GET" and path == "/guide.pdf"
            assert headers["Accept-Encoding"] == "identity"

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)


@pytest.mark.parametrize("status,headers", [
    (302, [("Location", "http://127.0.0.1/")]),
    (200, [("Content-Length", "999999999")]),
    (200, [("Content-Length", "2"), ("Content-Length", "3")]),
    (200, [("Content-Encoding", "gzip")]),
    (200, [("Transfer-Encoding", "chunked"), ("Content-Length", "3")]),
])
def test_response_rejected(monkeypatch, status, headers):
    mock_response(monkeypatch, status, headers)
    with pytest.raises(ValueError):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf"):
            pass


def test_direct_response_and_truncated_body(monkeypatch):
    mock_response(monkeypatch, 200, [("Content-Length", "3")])
    with pdf_transport.open_pdf("https://publisher.org/guide.pdf") as body:
        assert body.read(10) == b"abc"
        assert body.read(10) == b""
    mock_response(monkeypatch, 200, [("Content-Length", "4")])
    with pdf_transport.open_pdf("https://publisher.org/guide.pdf") as body:
        assert body.read(10) == b"abc"
        with pytest.raises(pdf_transport.RetryableDownloadError, match="incomplete"):
            body.read(10)


@pytest.mark.parametrize("status,retryable", [(408, True), (429, True), (500, True), (599, True), (403, False), (404, False)])
def test_retryable_http_statuses(monkeypatch, status, retryable):
    mock_response(monkeypatch, status, [("Retry-After", "900")])
    with pytest.raises(ValueError) as raised:
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf"):
            pytest.fail("HTTP errors must not open a stream")
    assert isinstance(raised.value, pdf_transport.RetryableDownloadError) == retryable
    if isinstance(raised.value, pdf_transport.RetryableDownloadError):
        assert raised.value.retry_after == 30


@pytest.mark.parametrize("value,delay", [("2", 2), ("-1", 0), ("1.5", 0),
    ("Thu, 01 Jan 1970 00:00:17 GMT", 7), ("Thu, 01 Jan 1970 00:00:05 GMT", 0)])
def test_retry_after(monkeypatch, value, delay):
    monkeypatch.setattr(pdf_transport.time, "time", lambda: 10)
    headers = Message()
    headers["Retry-After"] = value
    assert pdf_transport._retry_after(headers) == delay
    headers["Retry-After"] = "10"
    assert pdf_transport._retry_after(headers) == 0


@pytest.mark.parametrize("error,retryable", [(TimeoutError(), True), (ConnectionResetError(), True),
    (ssl.SSLCertVerificationError(), False), (socket.gaierror(socket.EAI_NONAME, "unknown"), False),
    (socket.gaierror(socket.EAI_AGAIN, "temporary"), True)])
def test_transport_network_classification(monkeypatch, error, retryable):
    def failing_dns(*args, **kwargs):
        raise error
    monkeypatch.setattr(socket, "getaddrinfo", failing_dns)
    with pytest.raises(pdf_transport.DownloadError) as raised:
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf"):
            pytest.fail("Expected connection failure")
    assert isinstance(raised.value, pdf_transport.RetryableDownloadError) == retryable


def test_retry_policy_exhaustion_and_final_error():
    attempts, waits = [], []
    def fail():
        attempts.append(len(attempts) + 1)
        raise pdf_transport.RetryableDownloadError(f"failure {attempts[-1]}")
    with pytest.raises(pdf_transport.RetryableDownloadError, match="failure 4"):
        pdf_transport.run_with_download_retries(fail, wait=waits.append)
    assert attempts == [1, 2, 3, 4]
    assert waits == [1, 2, 4]


def test_redirects_revalidate_each_host_and_close_responses(monkeypatch):
    resolved = []
    requests = []
    responses = [
        Response(302, [("Location", "https://cdn.publisher.org/next.pdf")], b""),
        Response(307, [("Location", "/final.pdf")], b""),
        Response(200, [("Content-Length", "3")], b"abc"),
    ]
    def dns(host, *args, **kwargs):
        resolved.append(host)
        return addresses("8.8.8.8" if host == "publisher.org" else "1.1.1.1")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    class Connection:
        def __init__(self, host, address, deadline):
            self.host = host
            assert address[-1][0] == ("8.8.8.8" if host == "publisher.org" else "1.1.1.1")
        def request(self, method, path, headers):
            requests.append((self.host, path))
        def getresponse(self):
            return responses[len(requests)-1]
        def close(self):
            pass
    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)
    with pdf_transport.open_pdf("https://publisher.org/guide.pdf") as body:
        assert body.read(10) == b"abc"
        assert responses[0].body.closed and responses[1].body.closed
    assert resolved == ["publisher.org", "cdn.publisher.org", "cdn.publisher.org"]
    assert requests == [("publisher.org", "/guide.pdf"), ("cdn.publisher.org", "/next.pdf"),
                        ("cdn.publisher.org", "/final.pdf")]
    assert all(response.body.closed for response in responses)


@pytest.mark.parametrize("location", ["https://internal.publisher.org/private.pdf",
                                     "https://publisher.org/guide.pdf",
                                     "http://publisher.org/other.pdf",
                                     "https://publisher.org/other.pdf?token=secret"])
def test_redirect_rejects_unsafe_or_repeated_target(monkeypatch, location):
    mock_response(monkeypatch, 302, [("Location", location)])
    resolved = []
    def dns(host, *args, **kwargs):
        resolved.append(host)
        return addresses("127.0.0.1" if host.startswith("internal.") else "8.8.8.8")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(ValueError):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf"):
            pytest.fail("Unsafe redirect must not yield a body")
    assert resolved == (["publisher.org", "internal.publisher.org"] if location.startswith("https://internal.") else ["publisher.org"])


def test_redirect_limit(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: addresses("8.8.8.8"))
    paths = []
    class Connection:
        def __init__(self, *args):
            pass
        def request(self, method, path, headers):
            paths.append(path)
        def getresponse(self):
            return Response(302, [("Location", f"/hop{len(paths)}.pdf")], b"")
        def close(self):
            pass
    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)
    with pytest.raises(ValueError, match="redirect limit"):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf"):
            pass
    assert len(paths) == pdf_transport.MAX_REDIRECTS + 1


@pytest.mark.parametrize("status,headers", [
    (200, [("Content-Length", "6"), ("ETag", '"v1"')]),
    (206, [("Content-Range", "bytes 0-2/6"), ("Content-Length", "3"), ("ETag", '"v1"')]),
    (206, [("Content-Range", "bytes 3-5/7"), ("Content-Length", "3"), ("ETag", '"v1"')]),
    (206, [("Content-Range", "bytes 3-5/6"), ("Content-Length", "2"), ("ETag", '"v1"')]),
    (206, [("Content-Range", "bytes 3-5/6"), ("Content-Length", "3"), ("ETag", '"v2"')]),
    (206, [("Content-Length", "3"), ("ETag", '"v1"')]),
])
def test_resume_rejects_ignored_range_mismatched_range_or_changed_etag(monkeypatch, status, headers):
    mock_response(monkeypatch, status, headers, b"def")
    with pytest.raises(ValueError):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf", format_name="zim",
                                     offset=3, validator='"v1"', limit=100, total_seconds=30):
            pytest.fail("Resume must not yield unvalidated bytes")


def test_resume_sends_range_and_if_range_and_checks_continuation(monkeypatch):
    mock_response(monkeypatch, 206,
                  [("Content-Range", "bytes 3-5/6"), ("Content-Length", "3"), ("ETag", '"v1"')],
                  b"def")
    # Capture actual request headers, including If-Range.
    response = Response(206, [("Content-Range", "bytes 3-5/6"),
                              ("Content-Length", "3"), ("ETag", '"v1"')], b"def")
    headers_seen = []
    class Connection:
        def __init__(self, *args):
            pass
        def request(self, method, path, headers):
            headers_seen.append(headers)
        def getresponse(self):
            return response
        def close(self):
            pass
    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)
    with pdf_transport.open_pdf("https://publisher.org/guide.pdf", format_name="zim",
                                offset=3, validator='"v1"', limit=100, total_seconds=30) as stream:
        assert stream.total_size == 6
        assert stream.read(1024) == b"def"
        assert stream.read(1024) == b""
    assert headers_seen[0]["Range"] == "bytes=3-"
    assert headers_seen[0]["If-Range"] == '"v1"'


def test_download_can_return_capture_time_validator_without_changing_default(monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def offline(url, **kwargs):
        class Body:
            etag = '"exact-get-etag"'
        yield Body()
    artifact = {"filename": "test." + "a" * 64 + ".pdf",
                "bytes": 123, "sha256": "a" * 64}
    monkeypatch.setattr(pdf_transport, "open_pdf", offline)
    monkeypatch.setattr(pdf_transport.pdf_source, "install_stream",
                        lambda source_id, stream, **kwargs: artifact)
    assert pdf_transport.download("test", "https://publisher.org/guide.pdf") == artifact
    assert pdf_transport.download("test", "https://publisher.org/guide.pdf",
                                  capture_validator=True) == (artifact, '"exact-get-etag"')

def test_restart_preserves_remaining_retry_budget():
    calls = []
    def attempt():
        calls.append(1)
        raise pdf_transport.RetryableDownloadError("fixture")
    with pytest.raises(pdf_transport.RetryableDownloadError):
        pdf_transport.run_with_download_retries(attempt, retries_used=2, wait=lambda _: False)
    assert len(calls) == max(0, len(pdf_transport.RETRY_DELAYS) - 2) + 1

@pytest.mark.parametrize("status,headers", [
    (200, [("Content-Length", "3"), ("ETag", '"wrong"')]),
    (200, [("Content-Length", "3")]),
    (302, [("Location", "https://publisher.org/other.pdf")]),
    (412, []),
])
def test_reviewed_document_refuses_changed_validator_or_redirect(monkeypatch, status, headers):
    mock_response(monkeypatch, status, headers)
    with pytest.raises(ValueError):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf", required_etag='"reviewed"'):
            pytest.fail("Unreviewed representation opened a transfer")


def test_reviewed_document_requires_exact_get_validator(monkeypatch):
    mock_response(monkeypatch, 200, [("Content-Length", "3"), ("ETag", '"reviewed"')])
    with pdf_transport.open_pdf("https://publisher.org/guide.pdf", required_etag='"reviewed"') as body:
        assert body.etag == '"reviewed"'
        assert body.read(10) == b"abc"


def test_pool_reuses_only_fully_consumed_response(monkeypatch):
    created = []
    monkeypatch.setattr(pdf_transport, "_public_addresses", lambda host: addresses("8.8.8.8"))

    class Socket:
        def settimeout(self, timeout):
            assert timeout > 0

    class Connection:
        def __init__(self, host, address, deadline):
            self.address = address
            self.sock = Socket()
            created.append(self)

        def request(self, method, path, headers):
            assert headers["Connection"] == "keep-alive"

        def getresponse(self):
            response = Response(200, [("Content-Length", "3")], b"abc")
            response.will_close = False
            return response

        def close(self):
            self.sock = None

    monkeypatch.setattr(pdf_transport, "_PinnedHTTPS", Connection)
    with pdf_transport.ConnectionPool() as pool:
        for _ in range(2):
            with pdf_transport.open_pdf("https://publisher.org/guide.pdf", pool=pool) as stream:
                assert stream.read(10) == b"abc"
                assert stream.read(10) == b""
        assert len(created) == 1
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf", pool=pool):
            pass
        assert created[0].sock is None
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf", pool=pool) as stream:
            stream.read(10)
            stream.read(10)
        assert len(created) == 2
    assert created[1].sock is None


def test_pool_revalidates_dns_before_reuse(monkeypatch):
    mock_response(monkeypatch, 200, [("Content-Length", "3")])
    # A pool must not be consulted at all if the current DNS answer is unsafe.
    class Pool:
        def take(self, *args):
            pytest.fail("Unsafe DNS must be rejected before connection lookup")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("192.168.1.1"))
    with pytest.raises(ValueError, match="non-public"):
        with pdf_transport.open_pdf("https://publisher.org/guide.pdf", pool=Pool()):
            pass
