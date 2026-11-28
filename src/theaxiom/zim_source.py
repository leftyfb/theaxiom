"""Streaming ZIM installation primitive; no HTTP routes or publication side effects."""
from contextlib import nullcontext
import hashlib
import os
import re
import struct
import time
from uuid import uuid4

from . import catalog_store, pdf_source, pdf_transport, storage, storage_settings

from . import structured_source

ARCHIVE_FORMATS = {"zim", "pmtiles"} | structured_source.FORMATS

def byte_limit(kind):
    return structured_source.MAX_BYTES if kind in structured_source.FORMATS else MAX_BYTES

def minimum_bytes(kind):
    return 1 if kind in structured_source.FORMATS else 96

MAX_BYTES = 2**40  # explicit 1 TiB ceiling; available storage may be lower
CHUNK = 1024 * 1024
TOTAL_SECONDS = 48 * 3600


def candidate(config):
    """URL is required; expected size and SHA-256 are independently optional."""
    url = config.get("source_url", "")
    size = config.get("expected_size_bytes", "")
    digest = config.get("sha256", "")
    if config.get("format") not in ARCHIVE_FORMATS or not pdf_source.valid_source_url(url):
        raise ValueError("ZIM download requires a direct plain HTTPS URL")
    if size and (not re.fullmatch(r"[1-9][0-9]{0,19}", size) or int(size) > byte_limit(config["format"])):
        raise ValueError("Expected ZIM size exceeds supported limits")
    if digest and not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("Invalid expected ZIM SHA-256")
    publisher_sha1(config)
    md5 = publisher_md5(config)
    if config["format"] == "pmtiles" and (not size or not (md5 or digest)):
        raise ValueError("PMTiles requires expected size and a publisher checksum")
    if config["format"] in structured_source.FORMATS:
        structured_source.options(config)
    return url, int(size) if size else None, digest.lower() if digest else None


def publisher_sha1(config):
    value = config.get("publisher_sha1", "")
    if not isinstance(value, str) or value and not re.fullmatch(r"[a-fA-F0-9]{40}", value):
        raise ValueError("Invalid publisher SHA-1")
    return value.lower()


def publisher_md5(config):
    value = config.get("publisher_md5", "")
    if not isinstance(value, str) or value and not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError("Invalid publisher MD5")
    return value.lower()


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
            or (titles not in (0, 0xffffffffffffffff) and not 80 <= titles <= checksum - 4 * articles)):
        raise ValueError("Invalid ZIM header or index bounds")


def install_stream(source_id, config, stream, *, progress=lambda current, total: None):
    """Install a verified byte snapshot on the configured mount, retaining prior versions.

    SHA-256 is an observed identity, not independent upstream authenticity.
    An optional expected digest is checked before promotion.
    """
    if not catalog_store.ID_RE.fullmatch(source_id):
        raise ValueError("Invalid source ID")
    url, expected, expected_digest = candidate(config)
    kind = config["format"]
    reported = getattr(stream, "length", None)
    if reported is not None and (type(reported) is not int or not 0 < reported <= byte_limit(kind)):
        raise ValueError("Invalid server ZIM size")
    if expected is not None and reported is not None and expected != reported:
        raise ValueError("ZIM server size differs from expected size")
    total = expected or reported
    settings = storage_settings.load_settings()
    with (pdf_source._directory("staging", writing=True, format_name="zim") as (staging, smount, _),
          pdf_source._directory("library", writing=True, format_name=kind) as (library, lmount, reserve)):
        if smount != lmount or storage_settings.load_settings() != settings:
            raise ValueError("ZIM storage changed during preparation")
        root = settings["storage_root"]
        def check_mount():
            mounts = storage.read_mounts()
            mount = storage._covering(pdf_source.Path(root), mounts)
            if (mount["id"] != smount or mount["read_only"]
                    or (settings["storage_mount"] and
                        (mount["target"] != settings["storage_mount"] or mount["source"] != settings["storage_source"]))
                    or any(storage._covering(pdf_source.Path(root) / branch / ("zim" if branch == "staging" else kind), mounts) != mount
                           for branch in ("staging", "library"))
                    or storage_settings.load_settings() != settings):
                raise ValueError("ZIM storage changed during acquisition")
        def available():
            capacity = os.fstatvfs(staging)
            if capacity.f_flag & os.ST_RDONLY:
                raise ValueError("ZIM storage is read-only")
            return capacity.f_bavail * capacity.f_frsize - reserve
        check_mount()
        if available() < (total or minimum_bytes(kind)):
            raise ValueError("Insufficient writable ZIM storage capacity")
        temp = ".download-" + uuid4().hex
        fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging)
        try:
            count = 0
            digest = hashlib.sha256()
            expected_sha1 = publisher_sha1(config)
            expected_md5 = publisher_md5(config)
            publisher_hash = hashlib.md5(usedforsecurity=False) if expected_md5 else None
            sha1 = hashlib.sha1(usedforsecurity=False) if expected_sha1 else None
            with os.fdopen(fd, "w+b") as target:
                while block := stream.read(CHUNK):
                    if count + len(block) > (total or byte_limit(kind)):
                        raise ValueError("ZIM exceeds download limit or expected size")
                    check_mount()
                    if len(block) > available():
                        raise ValueError("ZIM storage reserve would be exceeded")
                    target.write(block)
                    target.flush()
                    digest.update(block)
                    if sha1 is not None:
                        sha1.update(block)
                    if publisher_hash is not None:
                        publisher_hash.update(block)
                    count += len(block)
                    progress(count, total)
                if total is not None and count != total:
                    raise ValueError("Incomplete ZIM download")
                identity = digest.hexdigest()
                if sha1 is not None and sha1.hexdigest() != expected_sha1:
                    raise ValueError("ZIM publisher SHA-1 differs from expected digest")
                if expected_digest and identity != expected_digest:
                    raise ValueError("ZIM SHA-256 differs from expected digest")
                if publisher_hash is not None and publisher_hash.hexdigest() != expected_md5:
                    raise ValueError("Publisher MD5 differs from expected digest")
                target.seek(0)
                if kind == "pmtiles":
                    from . import pmtiles_source
                    pmtiles_source.check_header(target.read(127), count)
                elif kind in structured_source.FORMATS:
                    structured_source.parse(target, config)
                else:
                    check_header(target.read(80), count)
                if kind == "zim":
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
            name = source_id + "." + identity + "." + kind
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
    with pdf_transport.open_pdf(url, format_name="zim", limit=expected or byte_limit(config["format"]),
                                **({"media_type": structured_source.MEDIA_TYPES[config["format"]]} if config["format"] in structured_source.FORMATS else {}),
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


def complete_pinned_staging(config, offset):
    """A complete, independently pinned snapshot can be verified without HTTP."""
    _, expected, digest = candidate(config)
    return bool(expected and offset == expected and
                (digest or publisher_sha1(config) or publisher_md5(config)))


def download_resumable(source_id, config, *, offset=0, validator=None,
                       progress=lambda current, total, metadata: None,
                       stopped=lambda: False, promote_lock=None, promoting=lambda artifact, identity: None,
                       staging_id=None, write_lock=nullcontext, restart_from=0):
    """Persist source-scoped staged bytes and validate any resumed response before appending.

    An automatic restart without a validator may truncate only its matching,
    private staging file. No archive is published until full validation.
    """
    url, expected, expected_digest = candidate(config)
    kind = config["format"]
    if type(offset) is not int or offset < 0 or offset > byte_limit(kind):
        raise ValueError("Invalid saved archive offset")
    complete = complete_pinned_staging(config, offset)
    if offset and not validator and not complete:
        raise ValueError("Publisher supplied no stable ETag or Last-Modified; cannot safely resume")
    # A replacement shares the published source ID but never its staging inode.
    name = partial_name(staging_id if staging_id is not None else source_id)
    settings = storage_settings.load_settings()
    with (pdf_source._directory("staging", writing=True, format_name="zim") as (staging, mount_id, _),
          pdf_source._directory("library", writing=True, format_name=kind) as (library, library_mount, reserve)):
        if mount_id != library_mount or storage_settings.load_settings() != settings:
            raise ValueError("ZIM storage changed during preparation")

        root = settings["storage_root"]

        def check_mount():
            mounts = storage.read_mounts()
            mount = storage._covering(pdf_source.Path(root), mounts)
            if (mount["id"] != mount_id or mount["read_only"]
                    or (settings["storage_mount"] and
                        (mount["target"] != settings["storage_mount"] or mount["source"] != settings["storage_source"]))
                    or any(storage._covering(pdf_source.Path(root) / branch / ("zim" if branch == "staging" else kind), mounts) != mount
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
            if stat.st_size != (restart_from or offset):
                raise ValueError("Saved ZIM offset disagrees with staged file; manual recovery required")
            if expected and offset > expected:
                raise ValueError("Staged archive exceeds expected size; partial retained for manual review")
            if expected and offset == expected and not complete:
                raise ValueError("Complete staged archive lacks a pinned checksum; manual review required")
            if complete:
                return _verify_and_promote(
                    fd, staging, library, name, source_id, url, offset,
                    expected_digest, check_mount, available, stopped,
                    promote_lock, promoting, progress, expected_sha1=publisher_sha1(config),
                    kind=kind, expected_md5=publisher_md5(config), configuration=config)
            if available() < max(0, (expected or offset + minimum_bytes(kind)) - offset):
                raise ValueError("Insufficient writable ZIM storage capacity")
            if stopped():
                raise TransferStopped()
            # This network context re-validates every redirect destination and pins TLS
            # to the DNS result, just like normal document acquisition.
            with pdf_transport.open_pdf(url, format_name="zim", limit=expected or byte_limit(config["format"]),
                                **({"media_type": structured_source.MEDIA_TYPES[config["format"]]} if config["format"] in structured_source.FORMATS else {}),
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
                if restart_from:
                    with write_lock():
                        check_mount()
                        if stopped():
                            raise TransferStopped()
                        os.ftruncate(fd, 0)
                progress(offset, total, {"etag": stream.etag, "last_modified": stream.last_modified})
                count = offset
                with os.fdopen(os.dup(fd), "r+b") as target:
                    target.seek(offset)
                    while True:
                        if stopped():
                            raise TransferStopped()
                        # read1() may return only a TLS record or socket fragment.
                        # Aggregate outside the storage lock, retaining cancellation
                        # checks and the transport's per-read deadline enforcement.
                        batch = bytearray()
                        while len(batch) < CHUNK:
                            if stopped():
                                raise TransferStopped()
                            fragment = stream.read(CHUNK - len(batch))
                            if not fragment:
                                break
                            batch.extend(fragment)
                        if not batch:
                            break
                        if stopped():
                            raise TransferStopped()
                        block = batch
                        if count + len(block) > (total or byte_limit(kind)):
                            raise ValueError("ZIM exceeds download limit or expected size")
                        # Keep the capacity check and write together across ZIM workers.
                        with write_lock():
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
                    # Share the exact same integrity and atomic promotion path
                    # with explicit offline revalidation of retained archives.
                    return _verify_and_promote(
                        fd, staging, library, name, source_id, url, count,
                        expected_digest, check_mount, available, stopped,
                        promote_lock, promoting, progress, expected_sha1=publisher_sha1(config), kind=kind, expected_md5=publisher_md5(config), configuration=config)

        finally:
            os.close(fd)


def _verify_and_promote(fd, staging, library, name, source_id, url, count,
                        expected_digest, check_mount, available, stopped,
                        promote_lock, promoting, progress, *, expected_sha1="", kind="zim", expected_md5="", configuration=None):
    """Verify a staged inode and promote it without copying archive bytes."""
    from contextlib import nullcontext
    check_mount()
    last_report = 0.0

    def report_checksum(position, pass_number, *, boundary=False):
        nonlocal last_report
        now = time.monotonic()
        if boundary or now - last_report >= 1:
            progress(count, count, {"verifying": True, "checksum_bytes": position,
                                   "checksum_pass": pass_number, "checksum_passes": 2 if kind == "zim" else 1})
            last_report = now

    report_checksum(0, 1, boundary=True)
    with os.fdopen(os.dup(fd), "rb") as target:
        target.seek(0)
        sha = hashlib.sha256()
        sha1 = hashlib.sha1(usedforsecurity=False) if expected_sha1 else None
        publisher_hash = hashlib.md5(usedforsecurity=False) if expected_md5 else None
        position = 0
        while block := target.read(CHUNK):
            if stopped():
                raise TransferStopped()
            sha.update(block)
            if sha1 is not None:
                sha1.update(block)
            if publisher_hash is not None:
                publisher_hash.update(block)
            position += len(block)
            if position < count:
                report_checksum(position, 1)
        if position != count:
            raise ValueError("Staged ZIM changed during checksum verification")
        report_checksum(count, 1, boundary=True)
        if sha1 is not None and sha1.hexdigest() != expected_sha1:
            raise ValueError("ZIM publisher SHA-1 differs from expected digest")
        if publisher_hash is not None and publisher_hash.hexdigest() != expected_md5:
            raise ValueError("Publisher MD5 differs from expected digest")
        target.seek(0)
        if kind == "pmtiles":
            from . import pmtiles_source
            pmtiles_source.check_header(target.read(127), count)
        elif kind in structured_source.FORMATS:
            structured_source.parse(target, configuration)
        else:
            check_header(target.read(80), count)
        if kind == "zim":
            md5 = hashlib.md5(usedforsecurity=False)
            target.seek(0)
            remaining = count - 16
            report_checksum(0, 2, boundary=True)
            while remaining:
                if stopped():
                    raise TransferStopped()
                block = target.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("Incomplete staged ZIM")
                md5.update(block)
                remaining -= len(block)
                report_checksum(count - 16 - remaining, 2)
            footer = target.read(16)
            if len(footer) == 16:
                report_checksum(count, 2, boundary=True)
            if md5.digest() != footer:
                raise ValueError("ZIM internal checksum mismatch")
        digest = sha.hexdigest()
        if expected_digest and digest != expected_digest:
            raise ValueError("ZIM SHA-256 differs from expected digest")
    if stopped():
        raise TransferStopped()
    # Stage is durable before we publish its inode.
    os.fsync(fd)
    with promote_lock if promote_lock is not None else nullcontext():
        if stopped():
            raise TransferStopped()
        check_mount()
        if available() < 0:
            raise ValueError("ZIM storage reserve would be exceeded")
        pinned = os.fstat(fd)
        if pinned.st_size != count or pinned.st_nlink != 1:
            raise ValueError("Staged ZIM changed during final verification")
        filename = source_id + "." + digest + "." + kind
        artifact = {"filename": filename, "source_url": url, "bytes": count, "sha256": digest}
        promoting(artifact, promotion_identity(pinned))
        os.link(name, filename, src_dir_fd=staging, dst_dir_fd=library, follow_symlinks=False)
        linked = os.stat(filename, dir_fd=library, follow_symlinks=False)
        if ((linked.st_dev, linked.st_ino, linked.st_size) !=
                (pinned.st_dev, pinned.st_ino, count)):
            os.unlink(filename, dir_fd=library)
            os.fsync(library)
            raise ValueError("Staged ZIM changed during final promotion")
        os.fsync(library)
        os.unlink(name, dir_fd=staging)
        os.fsync(staging)
        return artifact


def promotion_identity(info):
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def revalidate_retained(source_id, config, *, expected_bytes, progress=lambda current, total, metadata: None,
                        stopped=lambda: False, promote_lock=None, promoting=lambda artifact, identity: None):
    """Verify an already complete retained staged ZIM, never contacting the network.

    Refuse unknown lengths: a prefix can have a valid header but is not a
    complete archive. Retain all bytes on any verification failure.
    """
    url, configured_size, expected_digest = candidate(config)
    kind = config["format"]
    if (type(expected_bytes) is not int or not minimum_bytes(kind) <= expected_bytes <= byte_limit(kind)
            or configured_size is not None and expected_bytes != configured_size):
        raise ValueError("A known complete expected ZIM size is required for local revalidation")
    settings = storage_settings.load_settings()
    with (pdf_source._directory("staging", writing=True, format_name="zim") as (staging, mount_id, _),
          pdf_source._directory("library", writing=True, format_name=kind) as (library, library_mount, reserve)):
        if mount_id != library_mount or storage_settings.load_settings() != settings:
            raise ValueError("ZIM storage changed during revalidation")

        def check_mount():
            mounts = storage.read_mounts()
            mount = storage._covering(pdf_source.Path(settings["storage_root"]), mounts)
            if (mount["id"] != mount_id or mount["read_only"]
                    or (settings["storage_mount"] and
                        (mount["target"] != settings["storage_mount"] or mount["source"] != settings["storage_source"]))
                    or any(storage._covering(pdf_source.Path(settings["storage_root"]) / branch / ("zim" if branch == "staging" else kind), mounts) != mount
                           for branch in ("staging", "library"))
                    or storage_settings.load_settings() != settings):
                raise ValueError("ZIM storage changed during revalidation")

        def available():
            capacity = os.fstatvfs(staging)
            if capacity.f_flag & os.ST_RDONLY:
                raise ValueError("ZIM storage is read-only")
            return capacity.f_bavail * capacity.f_frsize - reserve

        check_mount()
        fd = os.open(partial_name(source_id), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging)
        try:
            import stat as stat_module
            info = os.fstat(fd)
            if (not stat_module.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError("Unsafe staged ZIM file")
            if info.st_size != expected_bytes:
                raise ValueError("Retained ZIM is not the complete expected size")
            return _verify_and_promote(
                fd, staging, library, partial_name(source_id), source_id,
                url, expected_bytes, expected_digest, check_mount, available,
                stopped, promote_lock, promoting, progress, expected_sha1=publisher_sha1(config), kind=kind, expected_md5=publisher_md5(config), configuration=config)
        finally:
            os.close(fd)
