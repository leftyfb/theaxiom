"""Durable, private source activity journal (independent of acquisition queues)."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from . import storage_settings

ACTIONS = ("added", "removed", "edited", "published", "unpublished", "downloaded",
           "uploaded", "staged", "deleted", "updated", "rolled_back", "download_failed", "download_cancelled",
           "update_discovered", "update_queued", "update_started", "update_deferred", "update_failed", "update_cancelled")
TRIGGERS = ("manual", "batch", "automatic", "scheduled", "unknown")


def catalog_path():
    return Path(os.environ.get("THEAXIOM_SOURCE_CATALOG", storage_settings.SETTINGS_PATH.with_name("sources.json")))


def path():
    return catalog_path().with_name("source-activity.sqlite3")


def connect(target=None):
    target = target or path()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(target, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
               "event_id TEXT UNIQUE NOT NULL, timestamp TEXT NOT NULL, source_id TEXT NOT NULL, "
               "name TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, "
               "old_edition TEXT, new_edition TEXT, trigger TEXT NOT NULL)")
    if "reason" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
        try:
            db.execute("ALTER TABLE events ADD COLUMN reason TEXT")
        except sqlite3.OperationalError:
            if "reason" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
                raise
    db.execute("CREATE INDEX IF NOT EXISTS events_source ON events(source_id, sequence)")
    db.execute("CREATE TABLE IF NOT EXISTS consumers (name TEXT PRIMARY KEY, acknowledged INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS pending (event_id TEXT PRIMARY KEY, target TEXT NOT NULL, "
               "digest TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS seen (event_id TEXT PRIMARY KEY)")
    return db


def event_id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def source_name(sid):
    from . import catalog_store
    try:
        catalog = catalog_store.CatalogStore(catalog_path())
        return next((r["profile"]["title"] for r in catalog.get(initialize=False)["sources"]
                     if r["source_id"] == sid), sid)
    except (OSError, ValueError, catalog_store.CatalogError):
        return sid


def record(sid, action, *, name=None, old_edition=None, new_edition=None,
           trigger="manual", outcome="completed", key=None, target=None, reason=None):
    if action not in ACTIONS or trigger not in TRIGGERS or outcome not in {"completed", "failed", "cancelled"}:
        raise ValueError("Invalid activity event")
    if name is None:
        name = source_name(sid)
    event = dict(event_id=key or uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                 source_id=sid, name=name, action=action, outcome=outcome,
                 old_edition=old_edition, new_edition=new_edition, trigger=trigger, reason=reason)
    with closing(connect(target)) as db, db:
        _insert(db, event)


def _insert(db, event):
    event.setdefault("reason", None)
    if db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (event["event_id"],)).rowcount:
        db.execute("INSERT INTO events(event_id,timestamp,source_id,name,action,outcome,"
                   "old_edition,new_edition,trigger,reason) VALUES(:event_id,:timestamp,:source_id,:name,:action,:outcome,"
                   ":old_edition,:new_edition,:trigger,:reason)", event)


def prepare_write(file, payload, *, sid, name, action, old_edition=None, new_edition=None, key=None, target=None, trigger="manual", outcome="completed", append=False):
    """Persist a sanitized event intent before the lifecycle's atomic write.

    Caller holds the lifecycle lock. A crash after replace can be replayed
    from the exact committed byte digest, without storing catalog contents.
    """
    event = dict(event_id=key or uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                 source_id=sid, name=name, action=action, outcome=outcome,
                 old_edition=old_edition, new_edition=new_edition, trigger=trigger)
    with closing(connect(target)) as db, db:
        _reconcile(db, file=str(file), discard_uncommitted=not append)
        db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?)",
                   (event["event_id"], str(file), hashlib.sha256(payload).hexdigest(), json.dumps(event)))
    return event["event_id"]


def prepare_delete(directory_fd, names, *, sid, name=None):
    """Record intent to remove the exact already-opened managed filenames."""
    directory = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
    paths = [str(directory / filename) for filename in names]
    info = os.fstat(directory_fd)
    identity = [info.st_dev, info.st_ino]
    event = dict(event_id=uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                 source_id=sid, name=name or source_name(sid), action="deleted",
                 outcome="completed", old_edition=None, new_edition=None, trigger="manual")
    with closing(connect()) as db, db:
        _reconcile(db)
        # Retrying an interrupted partial deletion supersedes its incomplete intent.
        for prior in db.execute("SELECT event_id,payload FROM pending WHERE digest='absent'").fetchall():
            if json.loads(prior["payload"])["source_id"] == sid:
                db.execute("DELETE FROM pending WHERE event_id=?", (prior["event_id"],))
        db.execute("INSERT INTO pending VALUES (?,?,?,?)",
                   (event["event_id"], json.dumps({"paths": paths, "directory": identity}),
                    "absent", json.dumps(event)))
    return event["event_id"]


def finish_delete(key):
    with closing(connect()) as db, db:
        _reconcile(db, key=key)



def _reconcile(db, *, file=None, key=None, discard_uncommitted=False):
    rows = db.execute("SELECT * FROM pending" + (" WHERE target=?" if file else " WHERE event_id=?" if key else ""),
                      (file or key,) if file or key else ()).fetchall()
    for row in rows:
        if row["digest"] == "absent":
            target = json.loads(row["target"])
            paths = [Path(item) for item in target["paths"]]
            try:
                info = paths[0].parent.stat()
                committed = [info.st_dev, info.st_ino] == target["directory"] and all(
                    not os.path.lexists(item) for item in paths)
            except FileNotFoundError:
                committed = False
        else:
            try:
                committed = hashlib.sha256(Path(row["target"]).read_bytes()).hexdigest() == row["digest"]
            except FileNotFoundError:
                committed = False
        if committed:
            _insert(db, json.loads(row["payload"]))
        if committed or discard_uncommitted:
            db.execute("DELETE FROM pending WHERE event_id=?", (row["event_id"],))


def reconcile_write(file, *, target=None, discard_uncommitted=False):
    with closing(connect(target)) as db, db:
        _reconcile(db, file=str(file), discard_uncommitted=discard_uncommitted)


def query(source=None, action=None, before=None, limit=50):
    if action and action not in ACTIONS:
        raise ValueError("Unknown activity action")
    if not 1 <= limit <= 100 or before is not None and before < 1:
        raise ValueError("Invalid activity pagination")
    filters, args = [], []
    for column, value in (("source_id", source), ("action", action), ("sequence", before)):
        if value is not None:
            filters.append(column + (" < ?" if column == "sequence" else " = ?"))
            args.append(value)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with closing(connect()) as db, db:
        _reconcile(db)
        rows = [dict(row) for row in db.execute("SELECT * FROM events" + where +
                                              " ORDER BY sequence DESC LIMIT ?", (*args, limit + 1))]
        sources = [dict(row) for row in db.execute(
            "SELECT source_id,name FROM events WHERE sequence IN "
            "(SELECT MAX(sequence) FROM events GROUP BY source_id) ORDER BY name COLLATE NOCASE")]
    return {"events": rows[:limit], "next": rows[limit - 1]["sequence"] if len(rows) > limit else None,
            "sources": sources, "actions": list(ACTIONS)}


def acknowledge(consumer, sequence, *, retain=10000):
    """Registered consumers explicitly advance their durable high-water mark.

    Only events acknowledged by every registered consumer may be pruned.
    No automatic deletion occurs before a consumer is registered.
    """
    if not consumer or not 0 <= sequence or retain < 1:
        raise ValueError("Invalid activity acknowledgement")
    with closing(connect()) as db, db:
        maximum = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
        sequence = min(sequence, maximum)
        db.execute("INSERT INTO consumers VALUES (?,?) ON CONFLICT(name) DO UPDATE SET "
                   "acknowledged=MAX(acknowledged,excluded.acknowledged)", (consumer, sequence))
        db.execute("DELETE FROM events WHERE sequence <= (SELECT MIN(acknowledged) FROM consumers) "
                   "AND sequence <= COALESCE((SELECT sequence FROM events ORDER BY sequence DESC "
                   "LIMIT 1 OFFSET ?),0)", (retain,))
