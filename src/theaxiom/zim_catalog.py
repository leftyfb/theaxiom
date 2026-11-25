"""Fail-closed, source-independent Kiwix library manifest preparation.

This module DOES NOT start or expose a Kiwix listener. Deployment can feed its
generated library into a dedicated, publication-gated reader later. Only
explicitly published, locally verified completed ZIMs may enter the manifest.
"""
import argparse
from functools import cache
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from . import pdf_source, publication, storage_settings, zim_jobs

LANGUAGES = {
    "english": "eng", "spanish": "spa", "french": "fra",
    "german": "deu", "portuguese": "por", "italian": "ita",
}


def manifest_path():
    return storage_settings.SETTINGS_PATH.with_name("kiwix-library.xml")


def archive_uuid(stream):
    """Read the 16-byte ZIM header UUID (offset 8) without scanning the archive."""
    header = stream.read(24)
    if len(header) != 24 or header[:4] != b"ZIM\x04":
        raise ValueError("Invalid ZIM header")
    return str(uuid.UUID(bytes=header[8:24]))


def verified_books(records, enabled=None):
    """Return vetted Kiwix entries without touching installed content or its modes.

    The caller supplies catalog records rather than candidate strings. Never
    derive a filename from untrusted profile metadata or from the URL.
    """
    if enabled is None:
        enabled = publication.load()
    settings = storage_settings.load_settings()
    root = settings["storage_root"]
    if not root:
        return []
    candidates = [record for record in records
                  if record["configuration"].get("format") == "zim"
                  and enabled.get(record["source_id"], False)]
    result = []
    if candidates:
        with pdf_source._directory("library", format_name="zim") as (directory, mount, _):
            for record in candidates:
                sid = record["source_id"]
                config = record["configuration"]
                job = zim_jobs.status(sid)
                if not job or job.get("verified") is not True or job.get("active") is not False:
                    continue
                # Catalog URL edits are replacement intent, not a serving
                # switch. Keep the last verified installed book in the reader
                # until an explicit activation changes the serving job.
                artifact = zim_jobs.installed(job, job["configuration"])
                if not artifact:
                    continue
                digest = artifact["sha256"]
                size = artifact["bytes"]
                filename = artifact["filename"]
                if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                        or type(size) is not int or size < 96):
                    continue
                try:
                    with zim_jobs.open_installed_archive(directory, mount, job, artifact) as stream:
                        book_uuid = archive_uuid(stream)
                except (OSError, ValueError):
                    continue
                profile = record["profile"]
                title = profile.get("title", "").strip()
                if not title or len(title) > 256:
                    continue
                language = profile.get("language", "").strip().lower()
                language = LANGUAGES.get(language, language if re.fullmatch(r"[a-z]{3}", language) else "und")
                result.append({
                    "id": book_uuid, "path": str(Path(root) / "library" / "zim" / filename),
                    "title": title, "language": language,
                })
    return sorted(result, key=lambda book: book["id"])


def render_xml(records, enabled=None):
    """Generate deterministic XML with standard escaping and no arbitrary paths."""
    root = ET.Element("library", {"version": "20110515"})
    for book in verified_books(records, enabled):
        ET.SubElement(root, "book", book)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def write_manifest(records, enabled=None):
    """Persist a verified snapshot atomically in private application state."""
    content = render_xml(records, enabled)
    target = manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Kiwix --monitorLibrary compares integer-second mtimes (strictly >).
    # Two atomic manifest replacements in the same second may otherwise
    # NEVER be detected. Keep each replacement strictly newer than the
    # previous generation, even when updates are close together.
    try:
        previous_second = target.stat().st_mtime_ns // 1_000_000_000
    except FileNotFoundError:
        previous_second = -1
    generation_second = max(int(time.time()), previous_second + 1)
    fd, name = tempfile.mkstemp(prefix=".kiwix-library-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(name, ns=(generation_second * 1_000_000_000,
                            generation_second * 1_000_000_000))
        os.replace(name, target)
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(name).unlink(missing_ok=True)
    return target



# Written by the separate Kiwix reader startup handshake after it has verified
# that its OPDS catalog exactly matches the manifest. Missing runtime evidence
# disables serving; regenerating XML alone does not activate a reader.
RUNTIME_ACK = Path("/run/theaxiom-kiwix/manifest.sha256")
MAX_MANIFEST_BYTES = 256 * 1024


def active_manifest_matches(records, enabled=None, *, acknowledgement=None, expected_manifest=None):
    """Fail closed unless the reader acknowledged the exact current book set.

    This deliberately checks durable publisher intent on every call. An
    unpublish or a source-configuration change invalidates the old Kiwix
    instance immediately, even if its server process has not restarted.
    """
    try:
        expected = (expected_manifest() if expected_manifest is not None
                    else render_xml(records, enabled))
        if b"<book " not in expected or len(expected) > MAX_MANIFEST_BYTES:
            return False
        stored = manifest_path().read_bytes()
        if len(stored) > MAX_MANIFEST_BYTES or stored != expected:
            return False
        stamp = (acknowledgement or RUNTIME_ACK).read_bytes()
        return stamp == (hashlib.sha256(expected).hexdigest() + "\n").encode("ascii")
    except (OSError, ValueError, TypeError):
        return False




CONTENT_ROUTE = re.compile(r"/zim/content/(?P<book>[^/]+)(?:/|$)")


def content_name(book):
    """Kiwix content IDs derive from the installed ZIM filename, not source ID."""
    return Path(book["path"]).stem


def source_content_name(record):
    """Return the expected content ID only for a completed, verified archive."""
    try:
        job = zim_jobs.status(record["source_id"])
        if not job or job.get("verified") is not True or job.get("active") is not False:
            return None
        # A catalog URL change only prepares the next acquisition; the
        # current serving job remains authoritative until activation.
        artifact = zim_jobs.installed(job, job["configuration"])
        if not artifact:
            return None
        return Path(artifact["filename"]).stem
    except (OSError, ValueError, KeyError, TypeError):
        return None


def acknowledged_reader_books(records):
    """Fast request-time authorization against the startup-verified generation.

    No acquisition monitor, subprocess, archive reads, or NFS directory probes
    are performed here. Publication intent is reloaded for every request so
    unpublishing immediately denies access even before Kiwix reloads.
    """
    try:
        enabled = publication.load()
        stored = manifest_path().read_bytes()
        if len(stored) > MAX_MANIFEST_BYTES or not stored or b"<!DOCTYPE" in stored.upper() or b"<!ENTITY" in stored.upper():
            return {}
        stamp = RUNTIME_ACK.read_bytes()
        if stamp != (hashlib.sha256(stored).hexdigest() + "\n").encode("ascii"):
            return {}
        root = ET.fromstring(stored)
        if root.tag != "library" or root.get("version") != "20110515":
            return {}
        known = {record["source_id"]: record for record in records
                 if record["configuration"].get("format") == "zim"}
        result = {}
        seen = set()
        for book in root:
            if book.tag != "book" or set(book.attrib) != {"id", "path", "title", "language"}:
                return {}
            try:
                uuid.UUID(book.get("id", ""))
            except (ValueError, AttributeError):
                return {}
            filename = Path(book.get("path", "")).name
            sid = filename.split(".", 1)[0]
            if sid in seen or not enabled.get(sid, False) or sid not in known:
                return {}
            seen.add(sid)
            name = Path(filename).stem
            if not publication.GENERIC_BOOK_ID_RE.fullmatch(name) or not filename.endswith(".zim"):
                return {}
            record = known[sid]
            if book.get("title") != record["profile"].get("title", "").strip():
                return {}
            language = record["profile"].get("language", "").strip().lower()
            language = LANGUAGES.get(language, language if re.fullmatch(r"[a-z]{3}", language) else "und")
            if book.get("language") != language:
                return {}
            result[name] = sid
        return result
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError):
        return {}


def authorize_reader_uri(records, uri):
    """Authorize only acknowledged, currently published Kiwix book identities."""
    if not isinstance(uri, str) or len(uri) > 8192 or not uri.startswith("/zim/"):
        return False
    try:
        path = urlsplit(uri).path
        if not path.startswith("/zim/"):
            return False
        books = acknowledged_reader_books(records)
        if not books:
            return False
        if path.startswith("/zim/content/"):
            match = CONTENT_ROUTE.match(path)
            return bool(match and match.group("book") in books)
        return True
    except (OSError, ValueError, TypeError):
        return False


def generic_reader():
    """Kiwix listens on one configurable local port; default avoids Axiom's 8080."""
    value = os.environ.get("THEAXIOM_KIWIX_PORT", "8081")
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise ValueError("invalid Kiwix listener port")
    return {"path": "/zim/", "port": int(value)}


GENERIC_READER = generic_reader()


# A reconciliation diagnostic is advisory and tied to one exact manifest.
activation_failure = None


def status_for_catalog(records):
    """Build a status reader whose manifest snapshot lives for one catalog pass."""
    @cache
    def expected_manifest():
        return render_xml(records)

    @cache
    def manifest_matches():
        return active_manifest_matches(records, expected_manifest=expected_manifest)

    return lambda record: status(record, records, manifest_matches=manifest_matches,
                                 expected_manifest=expected_manifest)


def status(record, records, *, manifest_matches=None, expected_manifest=None):
    """User-visible publication state for a generic, acquired ZIM."""
    sid = record["source_id"]
    path = "/read/zim/" + sid
    result = {"enabled": False, "verified": False, "serving": False,
              "published": False, "path": path, "entry_path": path,
              "publication_reason": ""}
    try:
        config = record["configuration"]
        if config.get("format") != "zim":
            return result
        enabled = publication.load().get(sid, False)
        job = zim_jobs.status(sid)
        # The serving record remains authoritative while the catalog URL is
        # edited to prepare a replacement. Do not report the still-installed
        # published archive as missing until activation changes that record.
        artifact = (zim_jobs.installed(job, job["configuration"]) if job and
                    job.get("verified") is True and job.get("active") is False else None)
        verified = artifact is not None
        rights = bool(config.get("license", "").strip())
        # Distinguish missing prerequisites from a reader that has not yet
        # acknowledged the current manifest.
        acknowledged = False
        if verified and rights and enabled:
            acknowledged = (manifest_matches() if manifest_matches is not None
                            else active_manifest_matches(records))
        location = (publication.reader_location(GENERIC_READER, Path(artifact["filename"]).stem)
                    if acknowledged else None)
        ready = bool(location)
        diagnostic = activation_failure
        activation_reason = (
            diagnostic[1] if enabled and verified and rights and not acknowledged
            and diagnostic and diagnostic[0] == (expected_manifest() if expected_manifest is not None
                                                 else render_xml(records)) else ""
        )
        reason = ("Confirm permission to share this archive" if verified and not rights
                  else "Download and verify the archive before publishing" if enabled and not verified
                  else (activation_reason or "Waiting for Kiwix to acknowledge the current library catalog") if enabled and not acknowledged
                  else "Kiwix reader entry could not be resolved" if enabled and not ready
                  else "")
        return dict(result, enabled=enabled, verified=verified, serving=bool(ready),
                    published=bool(ready), ready_for_publication=verified,
                    publication_reason=reason,
                    requires_rights_acknowledgement=verified and not rights)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(result, error="ZIM publication state unavailable")


def main(argv=None):
    """Build the private manifest on demand; never start or reload Kiwix."""
    from . import catalog_store
    parser = argparse.ArgumentParser(description="Prepare the verified, published ZIM catalog; does not deploy")
    parser.add_argument("--catalog", type=Path, default=storage_settings.SETTINGS_PATH.with_name("sources.json"),
                        help="Path to the existing source catalog")
    args = parser.parse_args(argv)
    catalog = catalog_store.CatalogStore(args.catalog).get()
    # publication.load() and file identity checks fail closed before replacement.
    output = write_manifest(catalog["sources"])
    print(f"Verified published ZIM manifest written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
