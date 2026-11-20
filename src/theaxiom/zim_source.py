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


class TransferStopped(Exception):
    """Cooperative stop; caller decides whether staged bytes are retained."""


def partial_name(source_id):
    if not catalog_store.ID_RE.fullmatch(source_id):
        raise ValueError("Invalid source ID")
    return ".download-" + source_id + ".partial"


def discard_partial(source_id):
    """Remove only this source's staging file after checking the configured mount."""
    with pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
        os.unlink(partial_name(source_id), dir_fd=fd)
        os.fsync(fd)


def download_resumable(source_id, config, *, offset=0, validator=None,
                       progress=lambda current, total, metadata: None,
                       stopped=lambda: False):
    """Persist source-scoped staged bytes and validate any resumed response before appending.

    Only the caller may delete a partial, on an explicit cancellation. A failed
    or paused transfer retains it. No archive is published until full validation.
    """
    url, expected, expected_digest = candidate(config)
    if type(offset) is not int or offset < 0 or offset >= MAX_BYTES:
        raise ValueError("Invalid saved ZIM offset")
    if offset and not validator:
        raise ValueError("Publisher supplied no stable ETag or Last-Modified; cannot safely resume")
    name = partial_name(source_id)
    settings = storage_settings.load_settings()
    with (pdf_source._directory("staging", writing=True, format_name="zim") as (staging, mount_id, _),
          pdf_source._directory("library", writing=True, format_name="zim") as (library, library_mount, reserve)):
        if mount_id != library_mount or storage_settings.load_settings() != settings:
            raise ValueError("ZIM storage changed during preparation")

        root = settings["storage_root"]

        def check_mount():
            mounts = storage.read_mounts()
            mount = storage._covering(pdf_source.Path(root), mounts)
            if (mount["id"] != mount_id or mount["read_only"]
                    or (settings["storage_mount"] and
                        (mount["target"] != settings["storage_mount"] or mount["source"] != settings["storage_source"]))
                    or any(storage._covering(pdf_source.Path(root) / branch / "zim", mounts) != mount
                           for branch in ("staging", "library"))
                    or storage_settings.load_settings() != settings):
                raise ValueError("ZIM storage changed during acquisition")

        def available():
            cap = os.fstatvfs(staging)
            if cap.f_flag & os.ST_RDONLY:
                raise ValueError("ZIM storage is read-only")
            return cap.f_bavail * cap.f_frsize - reserve

        check_mount()
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=staging)
        try:
            stat = os.fstat(fd)
            import stat as stat_module
            if (not stat_module.S_ISREG(stat.st_mode) or stat.st_nlink != 1
                    or stat.st_uid != os.geteuid() or stat.st_mode & 0o077):
                raise ValueError("Unsafe staged ZIM file")
            if stat.st_size != offset:
                raise ValueError("Saved ZIM offset disagrees with staged file; manual recovery required")
            if expected and offset >= expected:
                raise ValueError("Staged ZIM offset exceeds expected total")
            if available() < max(0, (expected or offset + 96) - offset):
                raise ValueError("Insufficient writable ZIM storage capacity")
            if stopped():
                raise TransferStopped()
            # This network context re-validates every redirect destination and pins TLS
            # to the DNS result, just like normal document acquisition.
            with pdf_transport.open_pdf(url, format_name="zim", limit=expected or MAX_BYTES,
                                        total_seconds=TOTAL_SECONDS, offset=offset,
                                        validator=validator) as stream:
                total = stream.total_size or expected
                if expected and total and total != expected:
                    raise ValueError("Publisher ZIM size changed")
                if total and total <= offset:
                    raise ValueError("Invalid ZIM total size")
                if total and available() < total - offset:
                    raise ValueError("Insufficient writable ZIM storage capacity")
                observed_validator = stream.etag or stream.last_modified
                if offset and observed_validator != validator:
                    # A server can provide both ETag and Last-Modified; the
                    # explicit response check in the transport is authoritative.
                    if validator not in (stream.etag, stream.last_modified):
                        raise ValueError("Publisher validator changed")
                progress(offset, total, {"etag": stream.etag, "last_modified": stream.last_modified})
                count = offset
                with os.fdopen(os.dup(fd), "r+b") as target:
                    target.seek(offset)
                    while True:
                        if stopped():
                            raise TransferStopped()
                        block = stream.read(CHUNK)
                        if not block:
                            break
                        if count + len(block) > (total or MAX_BYTES):
                            raise ValueError("ZIM exceeds download limit or expected size")
                        check_mount()
                        if len(block) > available():
                            raise ValueError("ZIM storage reserve would be exceeded")
                        target.write(block)
                        target.flush()
                        count += len(block)
                        progress(count, total, {"etag": stream.etag, "last_modified": stream.last_modified})
                    if stopped():
                        raise TransferStopped()
                    if total is not None and count != total:
                        raise ValueError("Incomplete ZIM download")
                    # Verify the full retained file, including bytes from earlier sessions.
                    check_mount()
                    progress(count, total, {"verifying": True})
                    target.flush()
                    target.seek(0)
                    sha = hashlib.sha256()
                    md5 = hashlib.md5(usedforsecurity=False)
                    remaining = count - 16
                    if remaining < 80:
                        raise ValueError("Incomplete staged ZIM")
                    while block := target.read(CHUNK):
                        sha.update(block)
                    target.seek(0)
                    check_header(target.read(80), count)
                    target.seek(0)
                    while remaining:
                        block = target.read(min(CHUNK, remaining))
                        if not block:
                            raise ValueError("Incomplete staged ZIM")
                        md5.update(block)
                        remaining -= len(block)
                    if md5.digest() != target.read(16):
                        raise ValueError("ZIM internal checksum mismatch")
                    digest = sha.hexdigest()
                    if expected_digest and digest != expected_digest:
                        raise ValueError("ZIM SHA-256 differs from expected digest")
                    if stopped():
                        raise TransferStopped()
                    target.flush()
                    os.fsync(target.fileno())
                check_mount()
                if available() < 0:
                    raise ValueError("ZIM storage reserve would be exceeded")
                filename = source_id + "." + digest + ".zim"
                os.link(name, filename, src_dir_fd=staging, dst_dir_fd=library, follow_symlinks=False)
                os.fsync(library)
                os.unlink(name, dir_fd=staging)
                os.fsync(staging)
                return {"filename": filename, "source_url": url, "bytes": count, "sha256": digest}
        finally:
            os.close(fd)
