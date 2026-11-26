"""Bounded, direct HTTPS PDF transport. No proxies; every redirect is independently validated."""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import errno
import http.client
import ipaddress
import re
import socket
import ssl
import time
from typing import Callable, TypeVar
from urllib.parse import urlsplit, urljoin

from . import pdf_source
from .acquisition_queue import Cancelled

TIMEOUT = 10
TOTAL_SECONDS = 25
MAX_REDIRECTS = 3
RETRY_DELAYS = (1, 2, 4)
MAX_RETRY_AFTER = 30
STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e]{1,200}"')
T = TypeVar("T")


class DownloadError(ValueError):
    """Sanitized remote transport failure, distinct from local storage errors."""


class RetryableDownloadError(DownloadError):
    def __init__(self, message, *, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(headers):
    values = headers.get_all("Retry-After", [])
    if len(values) != 1 or len(values[0]) > 128:
        return 0
    value = values[0].strip()
    if re.fullmatch(r"[0-9]+", value):
        return min(MAX_RETRY_AFTER, int(value))
    stamp = _http_date(value)
    return min(MAX_RETRY_AFTER, max(0, stamp - time.time())) if stamp is not None else 0


def _http_date(value):
    try:
        date = parsedate_to_datetime(value)
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def resume_validator(etag, last_modified):
    if isinstance(etag, str) and STRONG_ETAG.fullmatch(etag):
        return etag
    if (isinstance(last_modified, str) and re.fullmatch(r"[\x20-\x7e]{1,200}", last_modified)
            and _http_date(last_modified) is not None):
        return last_modified
    return None


def _wait_for_retry(delay, stopped):
    if stopped is None:
        time.sleep(delay)
        return
    deadline = time.monotonic() + delay
    while not stopped():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))
    raise Cancelled()


def run_with_download_retries(attempt: Callable[[], T], *, stopped=None, on_retry=None, wait=None) -> T:
    """Run a transfer in the caller's existing job/lock, never retrying publication."""
    for count in range(len(RETRY_DELAYS) + 1):
        if stopped is not None and stopped():
            raise Cancelled()
        try:
            return attempt()
        except RetryableDownloadError as exc:
            if stopped is not None and stopped():
                raise Cancelled() from exc
            if count == len(RETRY_DELAYS):
                raise
            delay = max(RETRY_DELAYS[count], min(MAX_RETRY_AFTER, exc.retry_after))
            if on_retry is not None:
                on_retry(count + 1, delay, exc)
            if wait is None:
                _wait_for_retry(delay, stopped)
            elif wait(delay):  # threading.Event.wait for ZIM workers
                raise Cancelled() from exc
    raise AssertionError("Download retry loop did not terminate")


@contextmanager
def _network_errors():
    """Classify only exceptions raised by network operations, not by consumers."""
    try:
        yield
    except socket.gaierror as exc:
        error = RetryableDownloadError if exc.errno == socket.EAI_AGAIN else DownloadError
        raise error("Publisher DNS lookup failed") from exc
    except ssl.SSLEOFError as exc:
        raise RetryableDownloadError("Publisher interrupted the TLS connection") from exc
    except ssl.SSLError as exc:
        raise DownloadError("Publisher TLS validation or handshake failed") from exc
    except (TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
        raise RetryableDownloadError("Publisher connection interrupted or timed out") from exc
    except OSError as exc:
        error = (RetryableDownloadError if exc.errno in {
            errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH, errno.ENETRESET, errno.EHOSTUNREACH,
        } else DownloadError)
        raise error("Publisher network connection failed") from exc
    except http.client.HTTPException as exc:
        raise DownloadError("Invalid publisher HTTP response") from exc


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
        # read1 avoids waiting to fill a large buffer under a slow-drip response.
        with _network_errors():
            _remaining(self.deadline)
            data = self.response.read1(size)
            _remaining(self.deadline)
        self.count += len(data)
        if self.count > self.limit:
            raise ValueError("PDF exceeds download limit")
        if not data and self.length is not None and self.count < self.length:
            raise RetryableDownloadError("Publisher response body is incomplete")
        return data


@contextmanager
def open_pdf(url, *, format_name="pdf", limit=None, total_seconds=None, offset=0, validator=None):
    if format_name not in {"pdf", "epub", "zim"} or (format_name == "zim" and (limit is None or total_seconds is None)):
        raise ValueError("Unsupported document format")
    if offset and (format_name != "zim" or type(offset) is not int or not 0 < offset < limit
                   or not isinstance(validator, str) or not validator
                   or not re.fullmatch(r"[\x20-\x7e]{1,202}", validator)):
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
        with _network_errors():
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
            with _network_errors():
                connection.request("GET", canonical.path, headers=headers)
                response = connection.getresponse()
            try:
                with _network_errors():
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
                if response.status in (408, 429) or 500 <= response.status <= 599:
                    raise RetryableDownloadError(f"Publisher returned HTTP {response.status}",
                                                 retry_after=_retry_after(response.headers))
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
                etag = etags[0] if etags and STRONG_ETAG.fullmatch(etags[0]) else None
                last_modified = modified[0] if modified and re.fullmatch(r"[\x20-\x7e]{1,200}", modified[0]) else None
                if format_name == "zim" and last_modified and _http_date(last_modified) is None:
                    last_modified = None
                total_size = length
                if offset:
                    ranges = response.headers.get_all("Content-Range", [])
                    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", ranges[0]) if len(ranges) == 1 else None
                    if not match:
                        raise ValueError("Missing or malformed ZIM Content-Range")
                    first, last, total_size = map(int, match.groups())
                    if first != offset or last < first or total_size > limit or total_size != last + 1:
                        raise ValueError("ZIM Content-Range does not match saved offset")
                    if length is not None and length != last - first + 1:
                        raise ValueError("ZIM Content-Range length mismatch")
                    length = last - first + 1
                    if validator != etag and validator != last_modified:
                        raise ValueError("Publisher changed ZIM identity since partial download")
                yield _BoundedBody(response, deadline, length, limit - offset,
                                   etag=etag, last_modified=last_modified, total_size=total_size)
                return
            finally:
                response.close()
        finally:
            connection.close()


def download(source_id, url, *, expected_size=None, expected_sha256=None, capture_validator=False, stop=None):
    with open_pdf(url) as stream:
        artifact = pdf_source.install_stream(source_id, stream, expected_size=expected_size,
                                             expected_sha256=expected_sha256, **({"stop": stop} if stop else {}))
        # Only expose the validator from the exact successful GET that was
        # installed. It is not independent evidence of a newer edition.
        return (artifact, stream.etag) if capture_validator else artifact


def probe_document_metadata(url, *, if_none_match=None, total_seconds=15):
    """Read document headers without consuming a response body.

    Reuse the same pinned TLS/public DNS protections as document acquisition.
    Redirects are deliberately refused: following publisher-provided URLs
    requires separate trust review. No response metadata verifies local bytes
    or authorizes an update.
    """
    if not isinstance(url, str) or not pdf_source.valid_source_url(url):
        raise ValueError("Metadata check requires a plain HTTPS source URL")
    if (if_none_match is not None and
            (not isinstance(if_none_match, str)
             or re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', if_none_match) is None)):
        raise ValueError("Conditional HEAD requires a strong, safe ETag")
    if (type(total_seconds) not in (float, int)
            or not 0 < total_seconds <= TOTAL_SECONDS):
        raise ValueError("Invalid metadata check deadline")
    parsed = urlsplit(url)
    deadline = time.monotonic() + total_seconds
    addresses = _public_addresses(parsed.hostname)
    _remaining(deadline)
    headers = {"Accept-Encoding": "identity", "Connection": "close",
               "User-Agent": "The-Axiom document metadata check"}
    if if_none_match is not None:
        headers["If-None-Match"] = if_none_match
    result = _document_metadata_request(url, parsed, addresses[0], deadline, "HEAD", headers)
    if if_none_match and result["status"] in (403, 405, 501):
        # A publisher may serve GET while declining HEAD. Keep the same URL,
        # pinned address, validator and time budget. Never read the response body.
        head_status = result["status"]
        result = _document_metadata_request(
            url, parsed, addresses[0], deadline, "GET", dict(headers, Range="bytes=0-0"))
        result["head_status"] = head_status
    return result


def _document_metadata_request(url, parsed, address, deadline, method, headers):
    _remaining(deadline)
    connection = _PinnedHTTPS(parsed.hostname, address, deadline)
    try:
        connection.request(method, parsed.path, headers=headers)
        response = connection.getresponse()
        try:
            _remaining(deadline)
            # Never follow even an HTTPS redirect: the configured publisher
            # and its exact URL are part of the installed source identity.
            expected = (200, 304) if method == "HEAD" else (206, 304)
            if response.status not in expected:
                return {"source_url": url, "reachable": False,
                        "status": response.status, "etag": "",
                        "last_modified": "", "content_length": "",
                        "error": "metadata unavailable"}
            if response.getheader("Content-Encoding") not in (None, "identity"):
                raise ValueError("Encoded metadata response refused")
            if response.headers.get_all("Transfer-Encoding", []):
                raise ValueError("Unexpected transfer framing on metadata response")
            etags = response.headers.get_all("ETag", [])
            modified = response.headers.get_all("Last-Modified", [])
            lengths = response.headers.get_all("Content-Length", [])
            if max(len(etags), len(modified), len(lengths)) > 1:
                raise ValueError("Ambiguous metadata response headers")
            etag = (etags[0] if etags and
                    re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', etags[0])
                    else "")
            last_modified = (modified[0] if modified and
                             re.fullmatch(r"[\x20-\x7e]{1,200}", modified[0])
                             else "")
            size = ""
            if lengths:
                if (not lengths[0].isascii() or not lengths[0].isdecimal()
                        or len(lengths[0]) > 20):
                    raise ValueError("Invalid document metadata length")
                size = lengths[0]
            if response.status == 206:
                ranges = response.headers.get_all("Content-Range", [])
                content_range = (re.fullmatch(r"bytes 0-0/([1-9][0-9]{0,19})", ranges[0])
                                 if len(ranges) == 1 else None)
                if not content_range or size != "1":
                    raise ValueError("Unexpected document probe range")
                size = content_range[1]
            if response.status == 304 and "If-None-Match" not in headers:
                raise ValueError("Unsolicited conditional response")
            return {"source_url": url, "reachable": True, "status": response.status,
                    "etag": etag, "last_modified": last_modified,
                    "content_length": size, "error": ""}
        finally:
            response.close()
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise DownloadError("Document metadata connection failed or timed out") from exc
    finally:
        connection.close()
