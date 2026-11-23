"""Single-purpose Kiwix readiness handshake for the generic ZIM sidecar.

A successful OPDS match is required before issuing the runtime generation
acknowledgement. No Kiwix process is started or stopped by this module.
"""
import hashlib
import http.client
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from . import catalog_store, publication, storage_settings, zim_catalog

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_RESPONSE = 256 * 1024


class ReaderUnavailable(ValueError):
    """The loopback reader could not provide an HTTP response."""


def listed_book_ids(*, port=None):
    """Return a bounded set of strictly local OPDS content IDs."""
    connection = http.client.HTTPConnection("127.0.0.1", port if port is not None else zim_catalog.GENERIC_READER["port"], timeout=2)
    try:
        connection.request("GET", "/zim/catalog/v2/entries?count=101")
        response = connection.getresponse()
        if response.status != 200:
            raise ReaderUnavailable("Kiwix reader not ready")
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("Invalid Kiwix catalog response")
        root = ET.fromstring(data)
        entries = root.findall(ATOM + "entry")
        if not 1 <= len(entries) <= 100:
            raise ValueError("Kiwix catalog size is unexpected")
        result = set()
        for entry in entries:
            names = set()
            for link in entry.findall(ATOM + "link"):
                u = urlsplit(link.get("href", ""))
                prefix = "/zim/content/"
                if u.scheme or u.netloc or u.query or u.fragment or not u.path.startswith(prefix):
                    continue
                ident = u.path[len(prefix):].rstrip("/")
                if publication.valid_book_id(ident):
                    names.add(ident)
            if len(names) != 1:
                raise ValueError("Ambiguous Kiwix book identity")
            name = next(iter(names))
            if name in result:
                raise ValueError("Duplicate Kiwix book ID")
            result.add(name)
        return result
    except (OSError, http.client.RemoteDisconnected) as exc:
        raise ReaderUnavailable("Kiwix reader not ready") from exc
    except (ET.ParseError, http.client.HTTPException, UnicodeError) as exc:
        raise ValueError("Could not validate Kiwix OPDS catalog") from exc
    finally:
        connection.close()


def acknowledge(records, *, path=None, book_ids=None):
    """Record a loaded generation only when Kiwix proves the exact book set."""
    enabled = publication.load()
    books = zim_catalog.verified_books(records, enabled)
    expected_ids = {zim_catalog.content_name(book) for book in books}
    if not expected_ids:
        raise ValueError("No verified published ZIMs to serve")
    content = zim_catalog.render_xml(records, enabled)
    if zim_catalog.manifest_path().read_bytes() != content:
        raise ValueError("Configured Kiwix manifest differs from publication state")
    observed = listed_book_ids() if book_ids is None else set(book_ids)
    if observed != expected_ids:
        raise ValueError("Kiwix listener is serving an unexpected set of archives")
    # Recheck mutable publication state immediately before authorizing a
    # snapshot, so concurrent unpublish cannot create a valid stale marker.
    if zim_catalog.render_xml(records) != content:
        raise ValueError("Publication changed during Kiwix startup")
    destination = path or zim_catalog.RUNTIME_ACK
    if not destination.parent.is_dir():
        raise ValueError("Kiwix runtime directory is absent")
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".manifest-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((hashlib.sha256(content).hexdigest() + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        dir_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def _failure_category(exc):
    """Classify failed handshakes without including paths, URLs, or exception text."""
    if isinstance(exc, OSError):
        return "listener-or-filesystem-unavailable"
    message = str(exc)
    if message == "Kiwix reader not ready":
        return "listener-not-ready"
    if message in {"Could not validate Kiwix OPDS catalog",
                   "Invalid Kiwix catalog response", "Kiwix catalog size is unexpected",
                   "Ambiguous Kiwix book identity", "Duplicate Kiwix book ID"}:
        return "opds-invalid"
    if message == "Kiwix listener is serving an unexpected set of archives":
        return "opds-book-set-mismatch"
    if message in {"Configured Kiwix manifest differs from publication state",
                   "Publication changed during Kiwix startup"}:
        return "publication-state-changed"
    if message == "No verified published ZIMs to serve":
        return "no-published-archives"
    return "readiness-validation-failed"


def main():
    import sys
    catalog_path = Path(os.environ.get("THEAXIOM_SOURCE_CATALOG",
                        storage_settings.SETTINGS_PATH.with_name("sources.json")))
    records = catalog_store.CatalogStore(catalog_path).get()["sources"]
    deadline = time.monotonic() + 20
    attempts = 0
    while True:
        attempts += 1
        try:
            acknowledge(records)
            return 0
        except (OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                print(f"Kiwix readiness timed out after {attempts} attempts; "
                      f"last_failure={_failure_category(exc)}", file=sys.stderr)
                return 1
            time.sleep(0.25)


if __name__ == "__main__":
    raise SystemExit(main())
