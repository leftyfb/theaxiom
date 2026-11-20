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
import xml.etree.ElementTree as ET

from . import pdf_source, publication, storage_settings, zim_jobs

LANGUAGES = {
    "english": "eng", "spanish": "spa", "french": "fra",
    "german": "deu", "portuguese": "por", "italian": "ita",
}


def manifest_path():
    return storage_settings.SETTINGS_PATH.with_name("kiwix-library.xml")


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
    if not candidates:
        return []
    result = []
    with pdf_source._directory("library", format_name="zim") as (directory, mount, _):
        for record in candidates:
            sid = record["source_id"]
            config = record["configuration"]
            job = zim_jobs.status(sid)
            if not job or job.get("verified") is not True or job.get("active") is not False:
                continue
            artifact = zim_jobs.installed(job, config)
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
                with pdf_source._file(directory, mount, filename, size):
                    pass
            except (OSError, ValueError):
                continue
            profile = record["profile"]
            title = profile.get("title", "").strip()
            if not title or len(title) > 256:
                continue
            language = profile.get("language", "").strip().lower()
            language = LANGUAGES.get(language, language if re.fullmatch(r"[a-z]{3}", language) else "und")
            result.append({
                "id": sid, "path": str(Path(root) / "library" / "zim" / filename),
                "title": title, "language": language,
            })
    return sorted(result, key=lambda book: book["id"])


def render_xml(records, enabled=None):
    """Generate deterministic XML with standard escaping and no arbitrary paths."""
    root = ET.Element("library", {"version": "2"})
    for book in verified_books(records, enabled):
        ET.SubElement(root, "book", book)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def write_manifest(records, enabled=None):
    """Persist a verified snapshot atomically in private application state."""
    content = render_xml(records, enabled)
    target = manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".kiwix-library-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
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



GENERIC_READER = {"path": "/zim/", "port": 8082}


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
        artifact = (zim_jobs.installed(job, config) if job and
                    job.get("verified") is True and job.get("active") is False else None)
        verified = artifact is not None
        rights = bool(config.get("license", "").strip())
        ready = (verified and rights and enabled and active_manifest_matches(records)
                 and publication.reader_location(GENERIC_READER, sid) is not None)
        reason = ("Confirm permission to share this archive" if verified and not rights
                  else "Awaiting verified Kiwix reader activation" if enabled and not ready
                  else "")
        return dict(result, enabled=enabled, verified=verified, serving=bool(ready),
                    published=bool(ready), publication_reason=reason,
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
