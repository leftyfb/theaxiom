"""PDF lifecycle adapter for local and explicitly configured NFS storage. Catalog URLs are provenance, never fetched."""
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import uuid4

from . import acquisition_queue, catalog_store, storage, storage_settings

MAX_BYTES = 16 * 1024 * 1024
CHUNK = 128 * 1024
LOCAL_FILESYSTEMS = storage.LOCAL_FILESYSTEMS
NETWORK_FILESYSTEMS = storage.NETWORK_FILESYSTEMS
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
    # Directory modes, including 0777, are an operator-controlled policy.
    # Never relax ownership, mount identity, symlink or installed-file checks.
    if (not stat.S_ISDIR(info.st_mode) or _mount_id(fd) != mount_id
            or info.st_uid != os.geteuid()):
        raise ValueError("Managed content directories must be owned by the application user and remain on the configured mount")


@contextmanager
def _directory(branch, *, writing=False, format_name="pdf"):
    if format_name not in {"pdf", "epub", "zim"}:
        raise ValueError("Unsupported storage format")
    config = storage_settings.load_settings()
    root = Path(config["storage_root"])
    if not config["storage_root"] or root.anchor != "/":
        raise ValueError("PDFs require an explicitly configured storage root")
    path = root / branch / format_name
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
                if not writing or index != len(path.parts) - 2 or part != format_name:
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
def _file(directory, mount_id, filename, size, *, allowed_links=(1,)):
    fd = os.open(filename, FILES, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in allowed_links or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or _mount_id(fd) != mount_id or info.st_size != size):
            raise ValueError("PDF file is unsafe or does not match its expected size")
        yield stream


def preview_installed_sizes(source_ids, format_name):
    """Count retained, structurally safe document files without reading their contents."""
    if format_name not in {"pdf", "epub"}:
        raise ValueError("Unsupported document format")
    sizes = dict.fromkeys(source_ids, 0)
    try:
        with _directory("library", format_name=format_name) as (directory, mount, _):
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 10_000:
                        return {}
                    sid, _, suffix = entry.name.partition(".")
                    if sid not in sizes or sizes[sid] is None:
                        continue
                    if not re.fullmatch(r"[a-f0-9]{64}\." + format_name, suffix):
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                        if not 0 < size <= MAX_BYTES:
                            raise ValueError("Unexpected installed document size")
                        with _file(directory, mount, entry.name, size):
                            sizes[sid] += size
                    except (OSError, ValueError):
                        sizes[sid] = None
    except (OSError, ValueError):
        return {}
    return sizes


def unlink_managed_file(directory, stream, name):
    """Delete only the same exclusive file that was opened safely."""
    opened = os.fstat(stream.fileno())
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (current.st_nlink != 1 or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino or current.st_size != opened.st_size):
        raise ValueError("Managed file changed or is shared; deletion refused")
    os.unlink(name, dir_fd=directory)


@contextmanager
def downloaded_documents(sid, kind):
    """Hold descriptors for this source's current and retained document files."""
    if not catalog_store.ID_RE.fullmatch(sid) or kind not in {"pdf", "epub"}:
        raise ValueError("Invalid managed document source")
    pattern = re.compile(re.escape(sid) + r"\.[a-f0-9]{64}\." + kind)
    with _directory("library", writing=True, format_name=kind) as (directory, mount, _), ExitStack() as opened:
        files = []
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 10_000 or len(files) >= 100:
                    raise ValueError("Too many files to safely review this deletion")
                if not pattern.fullmatch(entry.name):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                if not 0 < size <= MAX_BYTES:
                    raise ValueError("Unexpected downloaded document size")
                stream = opened.enter_context(_file(directory, mount, entry.name, size))
                files.append((entry.name, stream, size))
        if not files:
            raise ValueError("No downloaded files to delete")
        yield directory, sorted(files, key=lambda row: row[0])


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



def simple_document_title(data):
    """Best-effort classic PDF Info title; never interpret page or stream data.

    Only a single bounded classic xref table and an uncompressed, simple
    Info dictionary are supported here. More complex metadata is deliberately
    left to a future full PDF parser. Missing/unsupported titles are normal.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        return None
    footer = re.search(rb"(?:^|[\r\n])startxref\s+([0-9]{1,10})\s+%%EOF[\x00\t\n\f\r ]*\Z", data[-4096:])
    if not footer:
        return None
    offset = int(footer[1])
    if not 9 <= offset < len(data) or len(data) - offset > 256 * 1024:
        return None
    table, sep, trailer = data[offset:].partition(b"trailer")
    if not sep or not table.startswith((b"xref\n", b"xref\r")):
        return None
    match = re.match(rb"\s*<<([^<>]{1,4096})>>", trailer)
    if not match or re.search(rb"/(?:Prev|XRefStm|Encrypt)\b", match[1]):
        return None
    refs = re.findall(rb"/Info\s+([0-9]{1,10})\s+([0-9]{1,5})\s+R\b", match[1])
    if len(refs) != 1:
        return None
    wanted, generation = map(int, refs[0])
    if not wanted:
        return None
    lines = table.splitlines()
    i, entries = 1, []
    while i < len(lines):
        header = re.fullmatch(rb"\s*([0-9]{1,10})\s+([0-9]{1,10})\s*", lines[i])
        if not header:
            return None
        first, count = map(int, header.groups())
        i += 1
        if count > len(lines) - i:
            return None
        if first <= wanted < first + count:
            entry = re.fullmatch(rb"([0-9]{10})\s+([0-9]{5})\s+n\s*", lines[i + wanted - first])
            if not entry or int(entry[2]) != generation:
                return None
            entries.append(int(entry[1]))
        i += count
    if len(entries) != 1 or not 9 <= entries[0] < offset:
        return None
    start = entries[0]
    obj = data[start:min(offset, start + 8192)]
    pattern = (str(wanted).encode() + rb"\s+" + str(generation).encode()
               + rb"\s+obj\s*<<([^<>]{1,4096})>>\s*endobj\b")
    info = re.match(pattern, obj)
    if not info:
        return None
    # Conservative subset: plain printable ASCII titles, not escaped or
    # nested strings; ignore ambiguous dictionaries or unsupported encodings.
    # Parse complete name/string pairs so /Title inside a string is not a key.
    pair = re.compile(rb"\s*/([A-Za-z][A-Za-z0-9]*)\s*\(([^()\\\r\n]{0,1024})\)")
    body, pos, values = info[1], 0, []
    while pos < len(body):
        if not body[pos:].strip():
            break
        field = pair.match(body, pos)
        if not field:
            return None
        if field[1] == b"Title":
            values.append(field[2])
        pos = field.end()
    if len(values) != 1 or not re.fullmatch(rb"[ -~]{1,512}", values[0]):
        return None
    title = " ".join(values[0].decode("ascii").split())
    if not title or title.casefold() in {"untitled", "untitled document", "document", "pdf", "unknown", "none"}:
        return None
    return title


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



def install_stream(source_id, stream, *, expected_size=None, expected_sha256=None, stop=None):
    return _install_stream(source_id, stream, expected_size=expected_size, expected_sha256=expected_sha256, stop=stop)


def _install_stream(source_id, stream, *, expected_size=None, expected_sha256=None,
                    format_name="pdf", verify=None, stop=None):
    """Install bounded incoming PDF bytes, returning their observed identity.

    The caller must authorize acquisition and persist provenance separately.
    A computed digest is not an independent publisher attestation.
    """
    if format_name not in {"pdf", "epub"} or (format_name == "epub" and verify is None):
        raise ValueError("Unsupported document format or missing validator")
    verify = verify or _verify
    if not catalog_store.ID_RE.fullmatch(source_id):
        raise ValueError("Invalid PDF source ID")
    if expected_size is not None and (type(expected_size) is not int or not 0 < expected_size <= MAX_BYTES):
        raise ValueError("Invalid expected PDF size")
    if expected_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise ValueError("Invalid expected PDF SHA-256")
    with (_directory("staging", writing=True, format_name=format_name) as (staging, smount, _),
          _directory("library", writing=True, format_name=format_name) as (library, lmount, minimum)):
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
                while True:
                    if stop and stop():
                        raise acquisition_queue.Cancelled()
                    block = stream.read(min(CHUNK, limit - count + 1))
                    if not block:
                        break
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
                artifact = {"filename": f"{source_id}.{observed}.{format_name}",
                            "bytes": count, "sha256": observed}
                target.seek(0)
                verify(target, artifact)
                target.flush()
                os.fsync(target.fileno())
            if stop and stop():
                raise acquisition_queue.Cancelled()
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
    result = dict(enabled=False, verified=False, serving=False, published=False, path=path, entry_path=path, publication_reason="")
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
