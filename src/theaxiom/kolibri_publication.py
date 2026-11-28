"""Fail-closed acknowledgement for the optional native reader.

The runtime and Axiom share a private state directory, not the public library
mount. A pending transaction closes both Nginx origins until native imports
have completed and the exact channel set is acknowledged.
"""
from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit, unquote, parse_qsl

from . import catalog_store, kolibri_scope

MAX_STATE = 64 * 1024
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
FILE = re.compile(r"([a-f0-9]{32})\.([a-z0-9]{1,16})\Z")


def _validate(records):
    if not isinstance(records, dict) or len(records) > 100:
        raise ValueError("Invalid Kolibri publication registry")
    channels = set()
    for sid, record in records.items():
        if not catalog_store.ID_RE.fullmatch(sid) or not isinstance(record, dict) or set(record) != {"channel_id", "version", "manifest_sha256"}:
            raise ValueError("Invalid Kolibri publication record")
        cid = kolibri_scope._id(record["channel_id"])
        if cid in channels or type(record["version"]) is not int or record["version"] < 0 or not DIGEST.fullmatch(record["manifest_sha256"]):
            raise ValueError("Invalid Kolibri publication identity")
        channels.add(cid)
    return records


class Registry:
    def __init__(self, root, database):
        self.root = Path(root)
        self.database = Path(database)
        self.state = self.root / "publications.json"
        self.pending = self.root / "publication-pending"
        self.lock = self.root / "publication.lock"

    def _write(self, path, data):
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".publication-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            self._sync_dir()
        finally:
            Path(temp).unlink(missing_ok=True)

    def _sync_dir(self):
        fd = os.open(self.root, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self):
        if not self.state.exists():
            return {}
        with self.state.open("rb") as stream:
            data = stream.read(MAX_STATE + 1)
        if len(data) > MAX_STATE:
            raise ValueError("Kolibri publication registry exceeds limit")
        return _validate(json.loads(data))

    @contextmanager
    def _database(self):
        if self.database.is_symlink() or not self.database.is_file():
            raise ValueError("Kolibri database unavailable")
        with closing(sqlite3.connect(self.database.absolute().as_uri()+"?mode=ro", uri=True, timeout=.5)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            yield conn

    def _matches(self, records, conn):
        expected = {(r["channel_id"], r["version"]) for r in records.values()}
        actual = set(conn.execute("SELECT id, version FROM content_channelmetadata LIMIT 101"))
        return expected == actual

    def acknowledged(self):
        try:
            if self.pending.exists():
                return {}
            records = self.read()
            if not records:
                return {}
            with self._database() as conn:
                if not self._matches(records, conn) or self.pending.exists():
                    return {}
            return records
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return {}

    @contextmanager
    def transaction(self):
        """Yield an acknowledgement callback; failures deliberately remain closed.

        The caller must run verified native mutations and verify selected files.
        An interrupted transaction is not cleared until an explicit recovery
        reconciles the actual native runtime with the trusted catalog.
        """
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.pending.exists():
                raise ValueError("Native publication recovery required")
            self._write(self.pending, b"pending\n")
            committed = False

            def acknowledge(records):
                nonlocal committed
                records = _validate(records)
                with self._database() as conn:
                    if not self._matches(records, conn):
                        raise ValueError("Native runtime differs from requested publications")
                self._write(self.state, json.dumps(records, sort_keys=True).encode())
                committed = True

            yield acknowledge
            if not committed:
                raise ValueError("Native publication was not acknowledged")
            self.pending.unlink()
            self._sync_dir()

    def authorize(self, uri, *, alternate=False):
        """Allow local reader assets only while a runtime is acknowledged.

        Deny direct channel database exports, external streaming selectors,
        and unavailable file IDs; both origins use the same private registry.
        """
        try:
            parsed = urlsplit(uri)
            path = unquote(parsed.path)
            if (parsed.scheme or parsed.netloc or parsed.fragment or "%" in path or "\\" in path
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(p in {".", ".."} for p in path.split("/"))):
                return False
            if any(key.lower() in {"baseurl", "no_available_filtering"}
                   for key, _ in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)):
                return False
            prefix = "/content/" if alternate else "/kolibri/content/"
            if alternate and not path.startswith(prefix):
                return False
            if not alternate and not path.startswith("/kolibri/"):
                return False
            tail = path.removeprefix(prefix) if path.startswith(prefix) else ""
            if tail.startswith("databases/"):
                return False
            filename = None
            if tail.startswith("storage/"):
                parts = tail.split("/")
                if len(parts) != 4 or not FILE.fullmatch(parts[3]) or parts[1:3] != list(parts[3][:2]):
                    return False
                filename = parts[3]
            elif tail.startswith("zipcontent/"):
                parts = tail.split("/")
                if len(parts) < 3 or not FILE.fullmatch(parts[1]):
                    return False
                filename = parts[1]
            if self.pending.exists():
                return False
            records = self.read()
            if not records:
                return False
            with self._database() as conn:
                if not self._matches(records, conn):
                    return False
                if filename:
                    checksum, extension = FILE.fullmatch(filename).groups()
                    if not conn.execute("""
                        SELECT 1 FROM content_localfile l
                        JOIN content_file f ON f.local_file_id=l.id
                        JOIN content_contentnode n ON n.id=f.contentnode_id
                        WHERE l.id=? AND l.extension=? AND l.available=1 AND n.available=1
                        LIMIT 1
                    """, (checksum, extension)).fetchone():
                        return False
                return not self.pending.exists()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False


def configured_registry():
    """Opt-in runtime locations; no service or private state created by reads."""
    root = os.environ.get("THEAXIOM_KOLIBRI_PUBLICATIONS")
    database = os.environ.get("THEAXIOM_KOLIBRI_DATABASE")
    if not root or not database or not Path(root).is_absolute() or not Path(database).is_absolute():
        return None
    return Registry(root, database)


def catalog_acknowledged(records, registry=None):
    registry = registry or configured_registry()
    if registry is None:
        return {}
    active = registry.acknowledged()
    configured = {r["source_id"]: r["configuration"].get("kolibri_channel_id")
                  for r in records if r["configuration"].get("format") == "kolibri"}
    # A profile removal or identity change cannot leave content visible.
    if any(configured.get(sid) != artifact["channel_id"] for sid, artifact in active.items()):
        return {}
    return active
