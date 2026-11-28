"""Verified native Kolibri disk-import package, without archive conversion.

The caller must supply an exclusively managed staging directory on validated
library storage, enforce its storage/mount policy and persist the returned
manifest SHA-256 in its trusted job ledger. No public service is modified here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor
import threading
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import kolibri_scope as scope, pdf_transport, pdf_source

STUDIO = "https://studio.learningequality.org/content/"
MANIFEST = "axiom-kolibri-manifest.json"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CHUNK = 1024 * 1024
MAX_MANIFEST = 64 * 1024 * 1024


@dataclass(frozen=True)
class Selection:
    channel_id: str
    nodes: tuple[str, ...]
    version: int
    metadata_sha256: str
    media_bytes: int
    allow_full: bool = False
    base_url: str = STUDIO

    def __post_init__(self):
        scope._id(self.channel_id)
        validate_base(self.base_url)
        if not isinstance(self.nodes, tuple) or not 0 < len(self.nodes) <= scope.MAX_SCOPE_NODES:
            raise scope.ScopeError("Select explicit educational topics.")
        for node in self.nodes:
            scope._id(node)
        if len(set(self.nodes)) != len(self.nodes):
            raise scope.ScopeError("Duplicate selected nodes.")
        if type(self.version) is not int or self.version < 0:
            raise scope.ScopeError("Invalid channel version.")
        if not isinstance(self.metadata_sha256, str) or not SHA256.fullmatch(self.metadata_sha256):
            raise scope.ScopeError("Missing pinned channel metadata checksum.")
        if type(self.media_bytes) is not int or self.media_bytes <= 0:
            raise scope.ScopeError("Missing reviewed selection size.")
        if type(self.allow_full) is not bool:
            raise scope.ScopeError("Invalid full-channel confirmation.")


def validate_base(base_url):
    if (not isinstance(base_url, str) or not base_url.endswith("/content/")
            or not pdf_source.valid_source_url(base_url + "databases/example.sqlite3")):
        raise scope.ScopeError("Use a plain HTTPS Kolibri publisher content endpoint.")
    return base_url


def source_base(config):
    cid = scope._id(config.get("kolibri_channel_id"))
    source = config.get("source_url") or STUDIO + "databases/" + cid + ".sqlite3"
    suffix = "databases/" + cid + ".sqlite3"
    if not isinstance(source, str) or not source.endswith(suffix):
        raise scope.ScopeError("Publisher metadata URL must match the configured Kolibri channel ID.")
    return validate_base(source[:-len(suffix)])


def metadata_url(channel_id, version=None, *, base_url=STUDIO):
    suffix = ""
    if version is not None:
        if type(version) is not int or version < 0:
            raise scope.ScopeError("Invalid channel version.")
        suffix = "-" + str(version)
    return validate_base(base_url) + "databases/" + scope._id(channel_id) + suffix + ".sqlite3"


def _download(url, path, limit, *, exact=None, md5=None, guard=lambda n: None,
              progress=lambda n: None, total_seconds=180):
    sha = hashlib.sha256()
    publisher = hashlib.md5(usedforsecurity=False)
    count = 0
    # The existing origin-pinned TLS transport accepts only validated public
    # HTTPS endpoints. The format argument controls transport policy, not file
    # format or rendering. Content is never executed by Axiom.
    with pdf_transport.open_pdf(url, format_name="zim", limit=limit,
                                total_seconds=total_seconds) as stream:
        with path.open("xb") as out:
            while block := stream.read(CHUNK):
                count += len(block)
                if count > limit:
                    raise scope.ScopeError("Publisher file exceeds reviewed size.")
                guard(len(block))
                out.write(block)
                sha.update(block)
                publisher.update(block)
                progress(count)
    if count == 0 or (exact is not None and count != exact):
        raise scope.ScopeError("Publisher file size mismatch.")
    if md5 is not None and publisher.hexdigest() != md5:
        raise scope.ScopeError("Publisher file checksum mismatch.")
    return sha.hexdigest(), count


def fetch_metadata(channel_id, target, *, version=None, digest=None, guard=lambda n: None, base_url=STUDIO):
    """Preview latest metadata or fetch an exact reviewed version."""
    if digest is not None and (not isinstance(digest, str) or not SHA256.fullmatch(digest)):
        raise scope.ScopeError("Invalid pinned channel checksum.")
    target = Path(target)
    actual, size = _download(metadata_url(channel_id, version, base_url=base_url), target,
                             scope.MAX_DATABASE_BYTES, guard=guard)
    if digest is not None and actual != digest:
        raise scope.ScopeError("Channel metadata changed since review.")
    channel = scope.inspect_channel(target, channel_id)
    if version is not None and channel.version != version:
        raise scope.ScopeError("Channel version changed since review.")
    return channel, actual, size


def _plan(database, selection):
    plan = scope.plan_scope(database, selection.channel_id, selection.nodes,
                            allow_full=selection.allow_full)
    if plan.channel.version != selection.version or plan.total_bytes != selection.media_bytes:
        raise scope.ScopeError("Channel selection changed since review.")
    if plan.missing_license_count:
        raise scope.ScopeError("Selection includes resources without publisher license metadata.")
    return plan


def _database(root, channel_id):
    return root / "content" / "databases" / (scope._id(channel_id) + ".sqlite3")


def _file(root, item):
    return root / "content" / "storage" / item.storage_path


def _record(item, digest):
    return {"path": item.storage_path, "size": item.size, "md5": item.checksum,
            "sha256": digest}


def _manifest(selection, metadata_size, records):
    return {"schema": 1, "selection": asdict(selection), "metadata_bytes": metadata_size,
            "files": records}


def acquire(root, selection, *, progress=lambda n, total: None, guard=lambda n: None):
    """Create a disk-import package for native Kolibri, including exercises.

    Download only the explicit scope. The manifest is written last; incomplete
    directories cannot pass verification. The caller owns retry/cleanup.
    Returns the local manifest SHA-256, to be retained outside shared storage.
    """
    root = Path(root)
    guard(scope.MAX_DATABASE_BYTES + selection.media_bytes)
    root.mkdir(exist_ok=False)
    database = _database(root, selection.channel_id)
    database.parent.mkdir(parents=True)
    _, _, metadata_size = fetch_metadata(selection.channel_id, database,
        version=selection.version, digest=selection.metadata_sha256, guard=guard, base_url=selection.base_url)
    plan = _plan(database, selection)
    total = plan.total_bytes
    progress(0, total)
    lock = threading.Lock()
    stopped = threading.Event()
    counts = {}
    done = 0

    def checked_guard(n):
        if stopped.is_set():
            raise scope.ScopeError("Concurrent acquisition stopped.")
        guard(n)

    def fetch(item):
        nonlocal done
        try:
            target = _file(root, item)
            target.parent.mkdir(parents=True, exist_ok=True)

            def report(n):
                with lock:
                    counts[item.storage_path] = max(n, counts.get(item.storage_path, 0))
                    progress(done + sum(counts.values()), total)

            def attempt():
                checked_guard(0)
                return _download(selection.base_url + "storage/" + item.storage_path,
                    target, item.size, exact=item.size, md5=item.checksum,
                    guard=checked_guard, total_seconds=3600, progress=report)

            def retry(number, delay, error):
                checked_guard(0)
                target.unlink(missing_ok=True)

            digest, size = pdf_transport.run_with_download_retries(
                attempt, stopped=stopped.is_set, on_retry=retry, wait=stopped.wait)
            with lock:
                counts.pop(item.storage_path, None)
                done += size
                progress(done + sum(counts.values()), total)
            return _record(item, digest)
        except BaseException:
            stopped.set()
            raise

    # map preserves the reviewed file order for deterministic manifests.
    # Wait for running workers before the caller can clean up the package.
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(fetch, plan.files))
    data = json.dumps(_manifest(selection, metadata_size, records), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MANIFEST:
        raise scope.ScopeError("Native package manifest exceeds limit.")
    with (root / MANIFEST).open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(data).hexdigest()


def _regular(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise scope.ScopeError("Native package contains a non-regular file.")
        return info.st_size
    except OSError as exc:
        raise scope.ScopeError("Native package file is missing.") from exc


def _hash(path, size, *, md5=None):
    if _regular(path) != size:
        raise scope.ScopeError("Native package file size mismatch.")
    sha = hashlib.sha256()
    publisher = hashlib.md5(usedforsecurity=False)
    count = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            count += len(block)
            if count > size:
                raise scope.ScopeError("Native package changed during verification.")
            sha.update(block)
            publisher.update(block)
    if count != size or (md5 is not None and publisher.hexdigest() != md5):
        raise scope.ScopeError("Native package publisher checksum mismatch.")
    return sha.hexdigest()


def _tree(root, expected):
    if root.is_symlink() or not root.is_dir():
        raise scope.ScopeError("Native package directory is not a real directory.")
    observed = set()
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(parent) / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise scope.ScopeError("Native package contains a linked directory.")
        for name in files:
            path = Path(parent) / name
            _regular(path)
            relative = path.relative_to(root).as_posix()
            if relative not in expected:
                raise scope.ScopeError("Native package contains unexpected files.")
            observed.add(relative)
    if observed != expected:
        raise scope.ScopeError("Native package is incomplete.")


def verify(root, selection, manifest_sha256):
    """Re-verify exact publisher scope and local digests before native import."""
    if not isinstance(manifest_sha256, str) or not SHA256.fullmatch(manifest_sha256):
        raise scope.ScopeError("Missing trusted package manifest digest.")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise scope.ScopeError("Native package directory is not a real directory.")
    path = root / MANIFEST
    size = _regular(path)
    if not 0 < size <= MAX_MANIFEST:
        raise scope.ScopeError("Native package manifest exceeds limit.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest_sha256:
        raise scope.ScopeError("Native package manifest checksum mismatch.")
    try:
        manifest = json.loads(data)
        metadata_size = manifest["metadata_bytes"]
        records = manifest["files"]
        if (manifest["schema"] != 1 or type(metadata_size) is not int
                or not 0 < metadata_size <= scope.MAX_DATABASE_BYTES
                or not isinstance(records, list) or len(records) > scope.MAX_FILES
                or manifest["selection"] != json.loads(json.dumps(asdict(selection)))):
            raise ValueError("Unexpected package selection")
    except (ValueError, TypeError, KeyError) as exc:
        raise scope.ScopeError("Invalid native package manifest.") from exc
    database = _database(root, selection.channel_id)
    # Check the directory tree before opening content paths, including parents.
    expected = {MANIFEST, database.relative_to(root).as_posix()}
    try:
        for record in records:
            rel = record["path"]
            if not isinstance(rel, str) or not re.fullmatch(
                    r"[0-9a-f]/[0-9a-f]/[0-9a-f]{32}\.[a-z0-9]{1,16}", rel):
                raise ValueError("Invalid native content path")
            expected.add("content/storage/" + rel)
    except (ValueError, TypeError, KeyError) as exc:
        raise scope.ScopeError("Invalid native package manifest paths.") from exc
    _tree(root, expected)
    if _hash(database, metadata_size) != selection.metadata_sha256:
        raise scope.ScopeError("Native package metadata checksum mismatch.")
    plan = _plan(database, selection)
    actual = []
    for item in plan.files:
        digest = _hash(_file(root, item), item.size, md5=item.checksum)
        actual.append(_record(item, digest))
    if records != actual:
        raise scope.ScopeError("Native package content differs from reviewed selection.")
    return plan


def import_commands(executable, root, selection):
    """Build argument vectors for upstream's local disk importer, never a shell.

    Call verify first; the caller must enforce public service quiescence and
    publication transaction boundaries. This helper does not execute commands.
    """
    return (
        [str(executable), "manage", "importchannel", "disk", selection.channel_id, str(root)],
        [str(executable), "manage", "importcontent", "--node_ids",
         ",".join(selection.nodes), "--fail-on-error", "disk", selection.channel_id, str(root)],
    )
