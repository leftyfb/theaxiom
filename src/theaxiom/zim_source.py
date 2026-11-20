"""Streaming ZIM installation primitive; no HTTP routes or publication side effects."""
import hashlib
import os
import re
import struct
from uuid import uuid4

from . import catalog_store, pdf_source, pdf_transport, storage, storage_settings

MAX_BYTES = 2**40  # explicit 1 TiB ceiling; available storage may be lower
CHUNK = 1024 * 1024
TOTAL_SECONDS = 48 * 3600


def candidate(config):
    """URL is required; expected size and SHA-256 are independently optional."""
    url = config.get("source_url", "")
    size = config.get("expected_size_bytes", "")
    digest = config.get("sha256", "")
    if config.get("format") != "zim" or not pdf_source.valid_source_url(url):
        raise ValueError("ZIM download requires a direct plain HTTPS URL")
    if size and (not re.fullmatch(r"[1-9][0-9]{0,19}", size) or int(size) > MAX_BYTES):
        raise ValueError("Expected ZIM size exceeds supported limits")
    if digest and not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("Invalid expected ZIM SHA-256")
    return url, int(size) if size else None, digest.lower() if digest else None


def check_header(header, size):
    # Basic structure only; independent Kiwix readability is required before publication.
    if len(header) != 80:
        raise ValueError("Incomplete ZIM header")
    magic, major, minor, uuid, articles, clusters, urls, titles, cluster_pos, mime, main, layout, checksum = struct.unpack("<IHH16sIIQQQQIIQ", header)
    if (magic != 0x044D495A or major not in (5, 6) or not articles or not clusters
            or not 80 <= checksum == size - 16
            or not 80 <= urls <= checksum - 8 * articles
            or not 80 <= cluster_pos <= checksum - 8 * clusters
            or not 80 <= mime < checksum
            or (titles != 0 and not 80 <= titles <= checksum - 4 * articles)):
        raise ValueError("Invalid ZIM header or index bounds")


def install_stream(source_id, config, stream, *, progress=lambda current, total: None):
    """Install a verified byte snapshot on the configured mount, retaining prior versions.

    SHA-256 is an observed identity, not independent upstream authenticity.
    An optional expected digest is checked before promotion.
    """
    if not catalog_store.ID_RE.fullmatch(source_id):
        raise ValueError("Invalid source ID")
    url, expected, expected_digest = candidate(config)
    reported = getattr(stream, "length", None)
    if reported is not None and (type(reported) is not int or not 0 < reported <= MAX_BYTES):
        raise ValueError("Invalid server ZIM size")
    if expected is not None and reported is not None and expected != reported:
        raise ValueError("ZIM server size differs from expected size")
    total = expected or reported
    settings = storage_settings.load_settings()
    with (pdf_source._directory("staging", writing=True, format_name="zim") as (staging, smount, _),
          pdf_source._directory("library", writing=True, format_name="zim") as (library, lmount, reserve)):
        if smount != lmount or storage_settings.load_settings() != settings:
            raise ValueError("ZIM storage changed during preparation")
        root = settings["storage_root"]
        def check_mount():
            mounts = storage.read_mounts()
            mount = storage._covering(pdf_source.Path(root), mounts)
            if (mount["id"] != smount or mount["read_only"]
                    or (settings["storage_mount"] and
                        (mount["target"] != settings["storage_mount"] or mount["source"] != settings["storage_source"]))
                    or any(storage._covering(pdf_source.Path(root) / branch / "zim", mounts) != mount
                           for branch in ("staging", "library"))
                    or storage_settings.load_settings() != settings):
                raise ValueError("ZIM storage changed during acquisition")
        def available():
            capacity = os.fstatvfs(staging)
            if capacity.f_flag & os.ST_RDONLY:
                raise ValueError("ZIM storage is read-only")
            return capacity.f_bavail * capacity.f_frsize - reserve
        check_mount()
        if available() < (total or 96):
            raise ValueError("Insufficient writable ZIM storage capacity")
        temp = ".download-" + uuid4().hex
        fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging)
        try:
            count = 0
            digest = hashlib.sha256()
            with os.fdopen(fd, "w+b") as target:
                while block := stream.read(CHUNK):
                    if count + len(block) > (total or MAX_BYTES):
                        raise ValueError("ZIM exceeds download limit or expected size")
                    check_mount()
                    if len(block) > available():
                        raise ValueError("ZIM storage reserve would be exceeded")
                    target.write(block)
                    target.flush()
                    digest.update(block)
                    count += len(block)
                    progress(count, total)
                if total is not None and count != total:
                    raise ValueError("Incomplete ZIM download")
                identity = digest.hexdigest()
                if expected_digest and identity != expected_digest:
                    raise ValueError("ZIM SHA-256 differs from expected digest")
                target.seek(0)
                check_header(target.read(80), count)
                # ZIM's trailing MD5 is a format-integrity check, not authentication.
                target.seek(0)
                actual = hashlib.md5(usedforsecurity=False)
                left = count - 16
                while left:
                    block = target.read(min(CHUNK, left))
                    if not block:
                        raise ValueError("Incomplete staged ZIM")
                    actual.update(block)
                    left -= len(block)
                if actual.digest() != target.read(16):
                    raise ValueError("ZIM internal checksum mismatch")
                os.fsync(target.fileno())
            check_mount()
            if available() < 0:
                raise ValueError("ZIM storage reserve would be exceeded")
            name = source_id + "." + identity + ".zim"
            # Never overwrite even an identically named existing archive.
            os.link(temp, name, src_dir_fd=staging, dst_dir_fd=library, follow_symlinks=False)
            os.unlink(temp, dir_fd=staging)
            os.fsync(library)
            os.fsync(staging)
            return {"filename": name, "source_url": url, "bytes": count, "sha256": identity}
        finally:
            try:
                os.unlink(temp, dir_fd=staging)
            except FileNotFoundError:
                pass


def download(source_id, config, *, progress=lambda current, total: None):
    url, expected, _ = candidate(config)
    with pdf_transport.open_pdf(url, format_name="zim", limit=expected or MAX_BYTES,
                                total_seconds=TOTAL_SECONDS) as stream:
        return install_stream(source_id, config, stream, progress=progress)
