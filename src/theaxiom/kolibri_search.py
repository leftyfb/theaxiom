"""Bounded, read-only search over the published native Kolibri database.

The caller must supply an acknowledged published channel and recheck that
acknowledgement after searching. A publisher export is not a live database.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode

from . import kolibri_scope


def search_nodes(database, channel_id, query, *, offset=0, limit=20, deadline=None):
    kolibri_scope._id(channel_id)
    if not isinstance(query, str) or not 0 < len(query.strip()) <= 160:
        raise ValueError("Invalid educational search query")
    if type(offset) is not int or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid educational search page")
    database = Path(database)
    if database.is_symlink() or not database.is_file():
        raise ValueError("Native reader database unavailable")
    deadline = min(deadline or time.monotonic() + 5, time.monotonic() + 5)
    # Treat user input literally; LIKE wildcards are not query syntax.
    query = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            rows = conn.execute("""
                SELECT n.id, n.title, coalesce(n.description, '')
                FROM content_contentnode AS n
                WHERE n.channel_id=? AND n.available=1 AND
                      (n.title LIKE ? ESCAPE '\\' OR n.description LIKE ? ESCAPE '\\')
                ORDER BY n.title COLLATE NOCASE, n.id
                LIMIT ? OFFSET ?
            """, (channel_id, query, query, limit + 1, offset)).fetchall()
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError("Native reader search timed out") from exc
        raise ValueError("Native reader search unavailable") from exc
    result = []
    for node_id, title, description in rows[:limit]:
        kolibri_scope._id(node_id)
        if isinstance(title, str) and title.strip():
            result.append((title, "/kolibri/viewcontent?" + urlencode({"node_id": node_id}),
                           description if isinstance(description, str) else ""))
    return result, len(rows) > limit
