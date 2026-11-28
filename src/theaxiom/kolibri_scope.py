"""Read-only scope planning for exported Kolibri channel databases.

This is a metadata-only building block, not an acquisition or publication adapter.
It never contacts a network, changes a channel, or grants redistribution rights.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import stat

MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_SCOPE_NODES = 64
MAX_RESOURCES = 100_000
MAX_FILES = 200_000
ID = re.compile(r"[0-9a-f]{32}\Z")
EXTENSION = re.compile(r"[a-z0-9]{1,16}\Z")


class ScopeError(ValueError):
    """The channel or scope cannot be safely planned."""


@dataclass(frozen=True)
class Channel:
    channel_id: str
    name: str
    version: int
    root_id: str


@dataclass(frozen=True)
class Topic:
    node_id: str
    title: str
    kind: str


@dataclass(frozen=True)
class ResourceFile:
    checksum: str
    extension: str
    size: int

    @property
    def storage_path(self) -> str:
        # Kolibri names files by their upstream MD5 and splits by first two digits.
        return f"{self.checksum[0]}/{self.checksum[1]}/{self.checksum}.{self.extension}"


@dataclass(frozen=True)
class Plan:
    channel: Channel
    selected_nodes: tuple[str, ...]
    resource_count: int
    files: tuple[ResourceFile, ...]
    total_bytes: int
    licenses: tuple[str, ...]
    missing_license_count: int


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ScopeError("Invalid Kolibri identifier.")
    return value


def _connect(path):
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DATABASE_BYTES:
            raise ScopeError("Channel metadata must be a bounded regular SQLite file.")
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise ScopeError("Channel metadata could not be opened.") from exc
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        # Reject views/virtual tables; only the documented export tables are used.
        tables = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
        for name in ("content_channelmetadata", "content_contentnode", "content_file", "content_localfile"):
            if not (tables.get(name) or "").lstrip().upper().startswith("CREATE TABLE"):
                raise ScopeError("Unsupported channel database schema.")
        return conn
    except (sqlite3.Error, ScopeError) as exc:
        conn.close()
        raise ScopeError("Unsupported channel database schema.") from exc


def _channel(conn, channel_id):
    channel_id = _id(channel_id)
    rows = conn.execute("SELECT id, name, version, root_id FROM content_channelmetadata LIMIT 2").fetchall()
    if len(rows) != 1:
        raise ScopeError("Expected exactly one channel.")
    cid, name, version, root = rows[0]
    if cid != channel_id or not isinstance(version, int) or version < 0:
        raise ScopeError("Unexpected channel identity or version.")
    root = _id(root)
    if conn.execute("SELECT 1 FROM content_contentnode WHERE id=? AND channel_id=? AND parent_id IS NULL AND kind='topic'", (root, cid)).fetchone() is None:
        raise ScopeError("Channel root is missing.")
    return Channel(cid, name, version, root)


def inspect_channel(path, channel_id):
    """Return metadata without inspecting or downloading any media."""
    try:
        with closing(_connect(path)) as conn:
            return _channel(conn, channel_id)
    except sqlite3.Error as exc:
        raise ScopeError("Unsupported channel database schema.") from exc


def children(path, channel_id, parent_id=None):
    """List direct children of a topic, preserving upstream tree order."""
    try:
        with closing(_connect(path)) as conn:
            channel = _channel(conn, channel_id)
            parent = channel.root_id if parent_id is None else _id(parent_id)
            if conn.execute("SELECT 1 FROM content_contentnode WHERE id=? AND channel_id=? AND kind='topic'", (parent, channel.channel_id)).fetchone() is None:
                raise ScopeError("Unknown channel topic.")
            rows = conn.execute(
                "SELECT id, title, kind FROM content_contentnode WHERE parent_id=? AND channel_id=? ORDER BY lft LIMIT 10001",
                (parent, channel.channel_id),
            ).fetchall()
            if len(rows) > 10000:
                raise ScopeError("Topic contains too many direct children.")
            return tuple(Topic(_id(nid), title, kind) for nid, title, kind in rows)
    except sqlite3.Error as exc:
        raise ScopeError("Unsupported channel database schema.") from exc


def plan_scope(path, channel_id, selected_nodes, *, allow_full=False):
    """Return unique media bytes and license inventory for explicit node selection.

    Counts upstream files, including alternative encodings and supplementary files,
    rather than assuming a particular renderer or resolution. MD5 is upstream
    identity/integrity metadata, not a signature or sufficient proof of provenance.
    """
    if not isinstance(selected_nodes, (list, tuple)) or not 0 < len(selected_nodes) <= MAX_SCOPE_NODES:
        raise ScopeError("Select at least one and at most 64 topics or lessons.")
    selected = tuple(dict.fromkeys(_id(nid) for nid in selected_nodes))
    try:
        with closing(_connect(path)) as conn:
            channel = _channel(conn, channel_id)
            if channel.root_id in selected and allow_full is not True:
                raise ScopeError("Full-channel selection requires explicit confirmation.")
            ranges = []
            for nid in selected:
                row = conn.execute(
                    "SELECT tree_id, lft, rght FROM content_contentnode WHERE id=? AND channel_id=?",
                    (nid, channel.channel_id),
                ).fetchone()
                if not row or not all(isinstance(v, int) for v in row) or not 0 < row[1] < row[2]:
                    raise ScopeError("Unknown or invalid channel node.")
                ranges.append(row)
            where = "n.channel_id=? AND (" + " OR ".join(
                "(n.tree_id=? AND n.lft>=? AND n.rght<=?)" for _ in ranges
            ) + ")"
            params = (channel.channel_id, *(v for row in ranges for v in row))
            resources = conn.execute(
                "SELECT n.license_name FROM content_contentnode n WHERE " + where +
                " AND n.kind!='topic' LIMIT ?", (*params, MAX_RESOURCES + 1)
            ).fetchall()
            if not resources or len(resources) > MAX_RESOURCES:
                raise ScopeError("Selection is empty or exceeds the resource limit.")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(content_localfile)")}
            size_column = "file_size_bigint" if "file_size_bigint" in columns else "file_size"
            if size_column not in columns:
                raise ScopeError("Unsupported file size metadata.")
            rows = conn.execute(
                "SELECT DISTINCT l.id, l.extension, l." + size_column +
                " FROM content_contentnode n JOIN content_file f ON f.contentnode_id=n.id "
                "LEFT JOIN content_localfile l ON l.id=f.local_file_id WHERE " + where +
                " ORDER BY l.id LIMIT ?", (*params, MAX_FILES + 1),
            ).fetchall()
            if not rows or len(rows) > MAX_FILES:
                raise ScopeError("Selection has no files or exceeds the file limit.")
            files = []
            for checksum, extension, size in rows:
                if not isinstance(checksum, str) or not ID.fullmatch(checksum):
                    raise ScopeError("Missing or invalid upstream file checksum.")
                if not isinstance(extension, str) or not EXTENSION.fullmatch(extension):
                    raise ScopeError("Invalid upstream file extension.")
                if not isinstance(size, int) or size <= 0:
                    raise ScopeError("Missing or invalid upstream file size.")
                files.append(ResourceFile(checksum, extension, size))
            licenses = tuple(sorted({value.strip() for (value,) in resources if isinstance(value, str) and value.strip()}))
            missing = sum(not isinstance(value, str) or not value.strip() for (value,) in resources)
            return Plan(channel, selected, len(resources), tuple(files),
                        sum(item.size for item in files), licenses, missing)
    except sqlite3.Error as exc:
        raise ScopeError("Unsupported channel database schema.") from exc
