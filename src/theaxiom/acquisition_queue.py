"""Persistent reservations and serial execution for confirmed acquisitions."""
from contextlib import closing, contextmanager
import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
from uuid import uuid4

from . import storage_settings

LOG = logging.getLogger(__name__)
PENDING = ("queued", "running")


class Cancelled(ValueError):
    pass


@contextmanager
def database():
    path = storage_settings.SETTINGS_PATH.with_name("acquisition-queue.sqlite3")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
            operation TEXT NOT NULL, configuration TEXT NOT NULL, storage TEXT NOT NULL,
            bytes INTEGER NOT NULL, state TEXT NOT NULL, stop INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS automatic_attempts (
            source_id TEXT NOT NULL, digest TEXT NOT NULL, job_id TEXT UNIQUE NOT NULL,
            ready_at REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
            PRIMARY KEY (source_id, digest)
        )""")
        yield db


def enqueue(jobs, available, settings):
    """Reserve a complete batch atomically against current and pending space."""
    if not jobs:
        raise ValueError("Select a source eligible for batch download")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT source_id, bytes, storage FROM jobs WHERE state IN (?, ?)", PENDING).fetchall()
        snapshot = json.dumps(settings, sort_keys=True)
        if available is not None and any(row["storage"] != snapshot for row in pending):
            raise ValueError("Pending downloads must be resolved after a storage settings change")
        if {row["source_id"] for row in pending}.intersection(job["source_id"] for job in jobs):
            raise ValueError("A selected source already has a pending batch download")
        if (available is not None
                and sum(row["bytes"] for row in pending) + sum(job["bytes"] for job in jobs) > available):
            raise ValueError("Insufficient space after pending download reservations")
        ids = []
        for job in jobs:
            job_id = uuid4().hex
            if job["operation"] == "automatic-update":
                result = db.execute("INSERT OR IGNORE INTO automatic_attempts (source_id, digest, job_id) VALUES (?, ?, ?)",
                                    (job["source_id"], job["configuration"]["digest"], job_id))
                if not result.rowcount:
                    raise ValueError("This publisher digest already has an automatic attempt")
            db.execute("""INSERT INTO jobs (id, source_id, title, operation, configuration, storage, bytes, state)
                          VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                       (job_id, job["source_id"], job["title"], job["operation"],
                        json.dumps(job["configuration"]), snapshot, job["bytes"]))
            ids.append(job_id)
        db.execute("""DELETE FROM jobs WHERE state NOT IN ('queued', 'running') AND rowid NOT IN (
                      SELECT rowid FROM jobs ORDER BY rowid DESC LIMIT 100)""")
        return ids


def status():
    with database() as db:
        return [dict(row) for row in db.execute(
            "SELECT id, source_id, title, operation, bytes, state, stop, detail FROM jobs ORDER BY rowid DESC")]


def automatic_attempts():
    with database() as db:
        return [dict(row) for row in db.execute(
            "SELECT source_id, digest, state FROM automatic_attempts ORDER BY rowid DESC")]


def cancel(job_id):
    with database() as db:
        result = db.execute("""UPDATE jobs SET stop = 1,
            state = CASE WHEN state = 'queued' THEN 'cancelled' ELSE state END
            WHERE id = ? AND state IN (?, ?)""", (job_id, *PENDING))
        if not result.rowcount:
            raise ValueError("Download is no longer pending")
        db.execute("""UPDATE automatic_attempts SET state = 'cancelled' WHERE job_id = ?
                   AND EXISTS (SELECT 1 FROM jobs WHERE id = ? AND state = 'cancelled')""", (job_id, job_id))


def stopping(job_id):
    with database() as db:
        return bool(db.execute("SELECT stop FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def run_next(execute, shutdown):
    """Run one job while holding a process-wide worker lock."""
    lock = storage_settings.SETTINGS_PATH.with_name("acquisition-queue.lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "wb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT jobs.* FROM jobs LEFT JOIN automatic_attempts ON jobs.id = job_id
                                WHERE jobs.state IN (?, ?) AND coalesce(ready_at, 0) <= ?
                                ORDER BY jobs.state = 'running' DESC, jobs.rowid LIMIT 1""",
                             (*PENDING, time.time())).fetchone()
            if job is None or shutdown.is_set():
                return False
            db.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job["id"],))
            db.execute("UPDATE automatic_attempts SET state = 'running' WHERE job_id = ?", (job["id"],))
        try:
            state, detail = execute(job, lambda: stopping(job["id"]), shutdown)
        except Cancelled:
            state, detail = "cancelled", ""
        except (OSError, ValueError) as exc:
            state, detail = "failed", str(exc)[:300]
        except Exception:
            LOG.exception("Batch acquisition failed")
            state, detail = "failed", "Acquisition failed; check server logs"
        if state != "running":
            deferred = state == "deferred"
            if deferred:
                state = "queued"
            with database() as db:
                db.execute("""UPDATE jobs SET state = CASE
                    WHEN ? = 'queued' AND stop = 1 THEN 'cancelled' ELSE ? END,
                    detail = ? WHERE id = ?""", (state, state, detail, job["id"]))
                db.execute("""UPDATE automatic_attempts SET ready_at = ?,
                           state = (SELECT state FROM jobs WHERE id = ?) WHERE job_id = ?""",
                           (time.time() + 60 if deferred else 0, job["id"], job["id"]))
        return state not in PENDING


def start(execute, poll=None):
    shutdown = threading.Event()

    def run():
        next_poll = 0
        while not shutdown.is_set():
            try:
                if poll is not None and time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 60
                    try:
                        poll()
                    except Exception:
                        LOG.exception("Automatic update poll failed")
                if run_next(execute, shutdown):
                    continue
            except (OSError, sqlite3.Error):
                LOG.exception("Batch queue unavailable")
            shutdown.wait(2)

    threading.Thread(target=run, daemon=True, name="batch-acquisition").start()
    return shutdown
