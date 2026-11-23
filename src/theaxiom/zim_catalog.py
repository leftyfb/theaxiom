"""Fail-closed, source-independent Kiwix library manifest preparation.

This module DOES NOT start or expose a Kiwix listener. Deployment can feed its
generated library into a dedicated, publication-gated reader later. Only
explicitly published, locally verified completed ZIMs may enter the manifest.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid
import stat
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


def legacy_pinned_entries():
    """Packaged, fully verified external acquisition capability definitions.

    These entries are an explicit compatibility adapter for archives installed
    before the generic ZIM ledger. No editable source profile may supply one.
    """
    from . import source_catalog
    result = {}
    for entry in source_catalog.builtin_sources():
        if entry.get("kind") != "zim" or entry.get("legacy_acquisition") != "systemd-monitor":
            continue
        sid, filename, digest, size = (entry.get("source_id"), entry.get("filename"),
                                       entry.get("sha256"), entry.get("bytes"))
        if (not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", sid)
                or not isinstance(filename, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,195}\.zim", filename)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int or size < 96):
            continue
        result[sid] = entry
    return result


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
                  if record["source_id"] not in publication.ROUTES
                  and record["configuration"].get("format") == "zim"
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
                        or type(size) is not int or size < 96
                        or filename != f"{sid}.{digest}.zim"):
                    continue
                # O_NOFOLLOW and verified mount, ownership, size and installed-file
                # mode checks live in the existing storage adapter.
                try:
                    with pdf_source._file(directory, mount, filename, size) as stream:
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
    # The compatibility capability is selected by immutable seed metadata,
    # never by source ID. Do not read/have to rehash a large installed ZIM.
    from . import acquisition_monitor
    for trusted in legacy_pinned_entries().values():
        if not enabled.get(trusted["source_id"], False):
            continue
        evidence = acquisition_monitor.collect(trusted)
        if (evidence.get("state") != "completed" or
                evidence.get("verified") is not True or evidence.get("active") is not False or
                evidence.get("filename") != trusted["filename"] or
                evidence.get("sha256") != trusted["sha256"] or
                evidence.get("expected_bytes") != trusted["bytes"]):
            continue
        trusted_path = Path(root) / "library" / "zim" / trusted["filename"]
        try:
            fd = os.open(trusted_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != trusted["bytes"]:
                    raise ValueError("Unexpected legacy ZIM archive")
                trusted_uuid = archive_uuid(stream)
        except (OSError, ValueError):
            continue
        result.append({"id": trusted_uuid, "path": str(trusted_path),
                       "title": trusted["title"], "language": trusted["language"]})
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


def active_manifest_matches(records, enabled=None, *, acknowledgement=None):
    """Fail closed unless the reader acknowledged the exact current book set.

    This deliberately checks durable publisher intent on every call. An
    unpublish or a source-configuration change invalidates the old Kiwix
    instance immediately, even if its server process has not restarted.
    """
    try:
        expected = render_xml(records, enabled)
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
        trusted_by_filename = {entry["filename"]: entry
                               for entry in legacy_pinned_entries().values()}
        known = {record["source_id"]: record for record in records}
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
            pinned = trusted_by_filename.get(filename)
            sid = pinned["source_id"] if pinned is not None else filename.split(".", 1)[0]
            if sid in seen or not enabled.get(sid, False):
                return {}
            seen.add(sid)
            name = Path(filename).stem
            if not publication.valid_book_id(name) or not filename.endswith(".zim"):
                return {}
            if pinned is not None:
                if (book.get("title") != pinned["title"] or
                        book.get("language") != pinned["language"]):
                    return {}
            else:
                record = known.get(sid)
                if not record or record["configuration"].get("format") != "zim":
                    return {}
                if not re.fullmatch(re.escape(sid) + r"\.[a-f0-9]{64}\.zim", filename):
                    return {}
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


def status(record, records):
    """User-visible publication state for a generic, acquired ZIM."""
    sid = record["source_id"]
    path = "/read/zim/" + sid
    result = {"enabled": False, "verified": False, "serving": False,
              "published": False, "path": path, "entry_path": path,
              "publication_reason": ""}
    try:
        config = record["configuration"]
        if sid in publication.ROUTES or config.get("format") != "zim":
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
        # acknowledged the current manifest. Do not probe archives or services.
        acknowledged = (active_manifest_matches(records)
                        if verified and rights and enabled else False)
        location = (publication.reader_location(GENERIC_READER, source_content_name(record))
                    if acknowledged else None)
        ready = bool(location)
        reason = ("Confirm permission to share this archive" if verified and not rights
                  else "Download and verify the archive before publishing" if enabled and not verified
                  else "Waiting for Kiwix to acknowledge the current library catalog" if enabled and not acknowledged
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
