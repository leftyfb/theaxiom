"""PDF lifecycle adapter for local and explicitly configured NFS storage. Catalog URLs are provenance, never fetched."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import uuid4

from . import catalog_store, storage, storage_settings

MAX_BYTES = 16 * 1024 * 1024
CHUNK = 128 * 1024
LOCAL_FILESYSTEMS = {"ext4", "ext3", "ext2", "xfs", "btrfs", "tmpfs", "zfs"}
NETWORK_FILESYSTEMS = {"nfs", "nfs4"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILES = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
# The existing frontend proxies /api/ to this application, not /read/.
ROUTE_PREFIX = "/api/library/pdf/"
ROUTE = re.compile(ROUTE_PREFIX + r"([a-z0-9-]{1,63})(/document\.pdf)?\Z")


def valid_source_url(url):
    # Deliberately require a plain HTTPS document URL (no credentials, query,
    # fragments, encoded path components, port overrides, or address literals).
    try:
        origin = urlsplit(url)
        host = origin.hostname or ""
        labels = host.split(".")
        valid_url = (
            url.startswith("https://") and origin.netloc.lower() == host
            and len(labels) >= 2 and re.fullmatch(r"[a-z]{2,63}", labels[-1])
            and labels[-1] not in {"local", "localhost", "internal", "onion", "test", "invalid", "example"}
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            and re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", origin.path)
            and not any(segment in {".", ".."} for segment in origin.path.split("/"))
            and url == "https://" + origin.netloc + origin.path
        )
    except ValueError:
        valid_url = False
    return bool(valid_url)


def pin(source_id, config, *, require_rights=True):
    """Return an import pin only for explicitly documented, bounded PDF content."""
    url = config.get("source_url", "")
    size, digest = config.get("expected_size_bytes", ""), config.get("sha256", "")
    valid_url = valid_source_url(url)
    if (not catalog_store.ID_RE.fullmatch(source_id) or config.get("format") != "pdf"
            or (url and not valid_url) or not config.get("provenance", "").strip()
            or (require_rights and not config.get("license", "").strip())
            or not re.fullmatch(r"[1-9][0-9]{0,7}", size) or int(size) > MAX_BYTES
            or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
        raise ValueError("PDF publication requires provenance, redistribution rights, size (up to 16 MiB) and SHA-256; any source URL must be plain HTTPS.")
    return {"filename": f"{source_id}.{digest.lower()}.pdf", "source_url": url,
            "bytes": int(size), "sha256": digest.lower()}


def _mount_id(fd):
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise ValueError("cannot identify PDF filesystem")


def _check_directory(fd, mount_id):
    info = os.fstat(fd)
    if (_mount_id(fd) != mount_id or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError("PDF directories must be owned by the application user, not group/world writable, and on the configured mount")


@contextmanager
def _directory(branch, *, writing=False):
    config = storage_settings.load_settings()
    root = Path(config["storage_root"])
    if not config["storage_root"] or root.anchor != "/":
        raise ValueError("PDFs require an explicitly configured storage root")
    path = root / branch / "pdf"
    mounts = storage.read_mounts()
    mount = storage._covering(root, mounts)
    required = config["storage_mount"]
    if required and (mount["target"] != required or mount["source"] != config["storage_source"]):
        raise ValueError("PDF required mount missing or replaced")
    allowed = LOCAL_FILESYSTEMS | (NETWORK_FILESYSTEMS if required else set())
    if (mount["type"] not in allowed or (writing and mount["read_only"])
            or storage._covering(path, mounts) != mount):
        raise ValueError("PDF storage must be on the configured mount without nested mounts")
    fd = os.open("/", DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            try:
                child = os.open(part, DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                # Create only the PDF leaf, on the already configured mount.
                # Never create the mount, storage root or parent directories.
                if not writing or index != len(path.parts) - 2 or part != "pdf":
                    raise
                if (_mount_id(fd) != mount["id"]
                        or storage._covering(path.parent, storage.read_mounts()) != mount):
                    raise ValueError("PDF parent directory is not on the configured mount")
                created = False
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, DIRECTORY, dir_fd=fd)
                if created:
                    try:
                        if _mount_id(child) != mount["id"] or os.fstat(child).st_uid != os.geteuid():
                            raise ValueError("New PDF directory has unexpected mount or owner")
                        os.fchmod(child, 0o700)
                    except BaseException:
                        os.close(child)
                        raise
            os.close(fd)
            fd = child
        _check_directory(fd, mount["id"])
        if storage._covering(path, storage.read_mounts()) != mount:
            raise ValueError("PDF mount changed during directory opening")
        yield fd, mount["id"], config["minimum_free_bytes"]
    finally:
        os.close(fd)


@contextmanager
def _file(directory, mount_id, filename, size):
    fd = os.open(filename, FILES, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or _mount_id(fd) != mount_id or info.st_size != size):
            raise ValueError("PDF file is unsafe or does not match its expected size")
        yield stream


def _verify(stream, artifact, destination=None):
    """Verify the pin and basic PDF framing, not PDF semantics or active content."""
    remaining = artifact["bytes"]
    digest = hashlib.sha256()
    first = stream.read(min(CHUNK, remaining))
    if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n]", first):
        raise ValueError("Invalid PDF header")
    block, tail = first, b""
    while remaining and block:
        remaining -= len(block)
        digest.update(block)
        tail = (tail + block)[-4096:]
        if destination is not None:
            destination.write(block)
        block = stream.read(min(CHUNK, remaining))
    if remaining or stream.read(1):
        raise ValueError("PDF size does not match the catalog pin")
    footer = re.search(rb"(?:^|[\r\n])startxref[ \t\r\n]+([0-9]{1,10})[ \t\r\n]+%%EOF[\x00\t\n\f\r ]*\Z", tail)
    if not footer:
        raise ValueError("Invalid PDF footer")
    xref = int(footer[1])
    if not 9 <= xref < artifact["bytes"] - len(tail) + footer.start():
        raise ValueError("Invalid PDF cross-reference offset")
    stream.seek(xref)
    if not re.match(rb"(?:xref[\r\n]|[0-9]+ [0-9]+ obj\b)", stream.read(64)):
        raise ValueError("Missing PDF cross-reference table or stream")
    if digest.hexdigest() != artifact["sha256"]:
        raise ValueError("PDF SHA-256 does not match the catalog pin")
    stream.seek(0)


def import_local(source_id, artifact):
    """Copy a fixed inbox filename to a private staging file and atomically promote."""
    with (_directory("staging", writing=True) as (staging, smount, _),
          _directory("library", writing=True) as (library, lmount, minimum)):
        if smount != lmount:
            raise ValueError("PDF staging and library must share one mount")
        capacity = os.fstatvfs(staging)
        if (capacity.f_flag & os.ST_RDONLY
                or capacity.f_bavail * capacity.f_frsize < minimum + artifact["bytes"]):
            raise ValueError("Insufficient writable PDF storage capacity")
        with _file(staging, smount, source_id + ".pdf", artifact["bytes"]) as source:
            temporary = ".import-" + uuid4().hex
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600, dir_fd=staging)
            try:
                with os.fdopen(fd, "w+b") as target:
                    _verify(source, artifact, target)
                    target.seek(0)
                    _verify(target, artifact)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, artifact["filename"], src_dir_fd=staging, dst_dir_fd=library)
                for directory in (library, staging):
                    try:
                        os.fsync(directory)
                    except OSError:
                        pass  # The already committed rename cannot be rolled back safely.
            finally:
                try:
                    os.unlink(temporary, dir_fd=staging)
                except FileNotFoundError:
                    pass



def install_stream(source_id, stream, *, expected_size=None, expected_sha256=None):
    """Install bounded incoming PDF bytes, returning their observed identity.

    The caller must authorize acquisition and persist provenance separately.
    A computed digest is not an independent publisher attestation.
    """
    if not catalog_store.ID_RE.fullmatch(source_id):
        raise ValueError("Invalid PDF source ID")
    if expected_size is not None and (type(expected_size) is not int or not 0 < expected_size <= MAX_BYTES):
        raise ValueError("Invalid expected PDF size")
    if expected_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise ValueError("Invalid expected PDF SHA-256")
    with (_directory("staging", writing=True) as (staging, smount, _),
          _directory("library", writing=True) as (library, lmount, minimum)):
        if smount != lmount:
            raise ValueError("PDF staging and library must share one mount")
        capacity = os.fstatvfs(staging)
        budget = min(MAX_BYTES, capacity.f_bavail * capacity.f_frsize - minimum)
        if capacity.f_flag & os.ST_RDONLY or budget < (expected_size or 1):
            raise ValueError("Insufficient writable PDF storage capacity")
        temporary = ".import-" + uuid4().hex
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=staging)
        try:
            with os.fdopen(fd, "w+b") as target:
                count = 0
                digest = hashlib.sha256()
                limit = min(budget, expected_size or MAX_BYTES)
                while block := stream.read(min(CHUNK, limit - count + 1)):
                    count += len(block)
                    if count > limit:
                        raise ValueError("PDF exceeds expected size or storage limit")
                    digest.update(block)
                    target.write(block)
                observed = digest.hexdigest()
                if expected_size is not None and count != expected_size:
                    raise ValueError("PDF size does not match the catalog pin")
                if expected_sha256 is not None and observed != expected_sha256.lower():
                    raise ValueError("PDF SHA-256 does not match the catalog pin")
                artifact = {"filename": f"{source_id}.{observed}.pdf",
                            "bytes": count, "sha256": observed}
                target.seek(0)
                _verify(target, artifact)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, artifact["filename"], src_dir_fd=staging, dst_dir_fd=library)
            for directory in (library, staging):
                try:
                    os.fsync(directory)
                except OSError:
                    pass
            return artifact
        finally:
            try:
                os.unlink(temporary, dir_fd=staging)
            except FileNotFoundError:
                pass


@contextmanager
def installed(artifact, *, verify=False):
    with _directory("library") as (directory, mount_id, _):
        with _file(directory, mount_id, artifact["filename"], artifact["bytes"]) as stream:
            if verify:
                _verify(stream, artifact)
            yield stream


def ready(artifact, *, verify=False):
    if artifact:
        try:
            with installed(artifact, verify=verify):
                return True
        except (OSError, ValueError):
            pass
    return False


def status(record, artifact):
    from . import publication
    sid = record["source_id"]
    path = ROUTE_PREFIX + sid
    result = dict(enabled=False, verified=False, serving=False, published=False, path=path, entry_path=path + "/document.pdf", publication_reason="")
    try:
        enabled = publication.load().get(sid, False)
        available = ready(artifact, verify=True)
        rights = bool(record["configuration"].get("license", "").strip())
        return dict(result, enabled=enabled, verified=available, serving=available and rights,
                    published=enabled and available and rights,
                    publication_reason="Review usage rights before publishing" if available and not rights else "",
                    requires_rights_acknowledgement=available and not rights)
    except (OSError, ValueError):
        return dict(result, error="publication state unavailable")
