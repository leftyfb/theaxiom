"""Bounded, direct HTTPS PDF transport. No proxies; every redirect is independently validated."""
from contextlib import contextmanager
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin

from . import pdf_source

TIMEOUT = 10
TOTAL_SECONDS = 25
MAX_REDIRECTS = 3


class DownloadError(ValueError):
    """Sanitized remote transport failure, distinct from local storage errors."""


def _public_addresses(host):
    results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    addresses = []
    for family, kind, proto, _, sockaddr in results:
        ip = ipaddress.ip_address(sockaddr[0])
        if (family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global
                or ip.is_multicast or ip.is_reserved or ip.is_loopback
                or ip.is_link_local or getattr(ip, "ipv4_mapped", None)
                or getattr(ip, "sixtofour", None) or getattr(ip, "teredo", None)
                or ip in ipaddress.ip_network("64:ff9b::/96")
                or ip in ipaddress.ip_network("64:ff9b:1::/48")):
            raise ValueError("PDF host resolves to a non-public or unsupported address")
        addresses.append((family, kind, proto, sockaddr))
    if not addresses:
        raise ValueError("PDF host has no usable public address")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, deadline):
        super().__init__(host, timeout=TIMEOUT, context=ssl.create_default_context())
        self.address = address
        self.deadline = deadline

    def connect(self):
        family, kind, proto, sockaddr = self.address
        raw = socket.socket(family, kind, proto)
        try:
            raw.settimeout(min(TIMEOUT, _remaining(self.deadline)))
            raw.connect(sockaddr)
            raw.settimeout(min(TIMEOUT, _remaining(self.deadline)))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("PDF download deadline exceeded")
    return left


class _BoundedBody:
    def __init__(self, response, deadline, length):
        self.response = response
        self.deadline = deadline
        self.length = length
        self.count = 0

    def read(self, size):
        _remaining(self.deadline)
        # read1 avoids waiting to fill a large buffer under a slow-drip response.
        try:
            data = self.response.read1(size)
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError("PDF network response interrupted or timed out") from exc
        _remaining(self.deadline)
        self.count += len(data)
        if self.count > pdf_source.MAX_BYTES:
            raise ValueError("PDF exceeds download limit")
        if not data and self.length is not None and self.count != self.length:
            raise ValueError("PDF response body is incomplete")
        return data


@contextmanager
def open_pdf(url):
    deadline = time.monotonic() + TOTAL_SECONDS
    visited = set()
    for hop in range(MAX_REDIRECTS + 1):
        if not isinstance(url, str) or not pdf_source.valid_source_url(url):
            raise ValueError("PDF download requires a direct plain HTTPS document URL")
        canonical = urlsplit(url)
        key = (canonical.hostname, canonical.path)
        if key in visited:
            raise ValueError("PDF redirect loop")
        visited.add(key)
        _remaining(deadline)
        addresses = _public_addresses(canonical.hostname)
        _remaining(deadline)
        # Each hop has its own DNS validation, numeric socket pin and TLS identity.
        connection = _PinnedHTTPS(canonical.hostname, addresses[0], deadline)
        try:
            connection.request("GET", canonical.path, headers={
                "Accept": "application/pdf, application/octet-stream",
                "Accept-Encoding": "identity", "Connection": "close",
                "User-Agent": "The-Axiom PDF acquisition",
            })
            response = connection.getresponse()
            try:
                _remaining(deadline)
                if response.status in (301, 302, 303, 307, 308):
                    locations = response.headers.get_all("Location", [])
                    if hop == MAX_REDIRECTS:
                        raise ValueError("PDF redirect limit exceeded")
                    if (len(locations) != 1 or not locations[0] or len(locations[0]) > 2048
                            or any(ord(c) <= 32 or ord(c) >= 127 for c in locations[0])
                            or "\\" in locations[0]):
                        raise ValueError("Invalid PDF redirect location")
                    url = urljoin(url, locations[0])
                    continue
                if response.status != 200:
                    raise ValueError(f"PDF server returned HTTP {response.status}")
                encoding = response.getheader("Content-Encoding")
                if encoding and encoding.strip().lower() != "identity":
                    raise ValueError("Encoded PDF responses are unsupported")
                lengths = response.headers.get_all("Content-Length", [])
                transfers = response.headers.get_all("Transfer-Encoding", [])
                if transfers and (lengths or transfers != ["chunked"]):
                    raise ValueError("Ambiguous PDF response framing")
                length = None
                if lengths:
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 10:
                        raise ValueError("Invalid PDF response size")
                    length = int(lengths[0])
                    if not 0 < length <= pdf_source.MAX_BYTES:
                        raise ValueError("PDF response size exceeds limits")
                yield _BoundedBody(response, deadline, length)
                return
            finally:
                response.close()
        finally:
            connection.close()


def download(source_id, url, *, expected_size=None, expected_sha256=None):
    from contextlib import ExitStack
    with ExitStack() as stack:
        try:
            stream = stack.enter_context(open_pdf(url))
        except socket.gaierror as exc:
            raise DownloadError("PDF publisher DNS lookup failed") from exc
        except ssl.SSLError as exc:
            raise DownloadError("PDF publisher TLS validation or handshake failed") from exc
        except TimeoutError as exc:
            raise DownloadError("PDF publisher connection timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError("PDF publisher network connection or HTTP response failed") from exc
        return pdf_source.install_stream(source_id, stream, expected_size=expected_size,
                                         expected_sha256=expected_sha256)
