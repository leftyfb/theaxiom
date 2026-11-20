"""Bounded, direct HTTPS PDF transport. No proxies; every redirect is independently validated."""
from contextlib import contextmanager
import http.client
import ipaddress
import re
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
    def __init__(self, response, deadline, length, limit=None, *, etag=None, last_modified=None, total_size=None):
        self.response = response
        self.etag = etag
        self.last_modified = last_modified
        self.total_size = total_size
        self.deadline = deadline
        self.length = length
        self.count = 0
        self.limit = pdf_source.MAX_BYTES if limit is None else limit

    def read(self, size):
        _remaining(self.deadline)
        # read1 avoids waiting to fill a large buffer under a slow-drip response.
        try:
            data = self.response.read1(size)
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError("PDF network response interrupted or timed out") from exc
        _remaining(self.deadline)
        self.count += len(data)
        if self.count > self.limit:
            raise ValueError("PDF exceeds download limit")
        if not data and self.length is not None and self.count != self.length:
            raise ValueError("PDF response body is incomplete")
        return data


@contextmanager
def open_pdf(url, *, format_name="pdf", limit=None, total_seconds=None, offset=0, validator=None):
    if format_name not in {"pdf", "epub", "zim"} or (format_name == "zim" and (limit is None or total_seconds is None)):
        raise ValueError("Unsupported document format")
    if offset and (format_name != "zim" or type(offset) is not int or not 0 < offset < limit
                   or not isinstance(validator, str) or not validator
                   or "\\r" in validator or "\\n" in validator or len(validator) > 256):
        raise ValueError("Safe ZIM resume requires a validated offset and upstream validator")
    limit = pdf_source.MAX_BYTES if limit is None else limit
    deadline = time.monotonic() + (TOTAL_SECONDS if total_seconds is None else total_seconds)
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
            headers = {
                "Accept": ({"epub": "application/epub+zip", "pdf": "application/pdf", "zim": "application/octet-stream"}[format_name]) + ", application/octet-stream",
                "Accept-Encoding": "identity", "Connection": "close",
                "User-Agent": "The-Axiom PDF acquisition",
            }
            if offset:
                headers["Range"] = f"bytes={offset}-"
                headers["If-Range"] = validator
            connection.request("GET", canonical.path, headers=headers)
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
                if offset:
                    if response.status == 200:
                        raise ValueError("Publisher ignored HTTP Range; partial archive preserved")
                    if response.status != 206:
                        raise ValueError(f"ZIM resume rejected by publisher (HTTP {response.status})")
                elif response.status != 200:
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
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > (20 if format_name == "zim" else 10):
                        raise ValueError("Invalid PDF response size")
                    length = int(lengths[0])
                    if not 0 < length <= limit:
                        raise ValueError("PDF response size exceeds limits")
                etags = response.headers.get_all("ETag", [])
                modified = response.headers.get_all("Last-Modified", [])
                if len(etags) > 1 or len(modified) > 1:
                    raise ValueError("Ambiguous publisher validator")
                etag = etags[0] if etags and re.fullmatch(r'"[\\x21\\x23-\\x7e]{1,200}"', etags[0]) else None
                last_modified = modified[0] if modified and re.fullmatch(r"[\\x20-\\x7e]{1,200}", modified[0]) else None
                total_size = length
                if offset:
                    ranges = response.headers.get_all("Content-Range", [])
                    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", ranges[0]) if len(ranges) == 1 else None
                    if not match:
                        raise ValueError("Missing or malformed ZIM Content-Range")
                    first, last, total_size = map(int, match.groups())
                    if first != offset or last < first or total_size > limit or total_size <= last:
                        raise ValueError("ZIM Content-Range does not match saved offset")
                    if length is not None and length != last - first + 1:
                        raise ValueError("ZIM Content-Range length mismatch")
                    if validator != etag and validator != last_modified:
                        raise ValueError("Publisher changed ZIM identity since partial download")
                yield _BoundedBody(response, deadline, length, limit - offset,
                                   etag=etag, last_modified=last_modified, total_size=total_size)
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
