"""Bounded structured journal entries for metadata-only source checks."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import update_check_store

LOGGER = logging.getLogger("theaxiom.update_diagnostics")
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False

_CURRENT = ContextVar("metadata_check", default=None)
_PHASES = {
    "installed_identity", "catalog_request", "catalog_parse", "catalog_filter",
    "candidate_selection", "metalink_request", "metalink_parse", "edition_comparison",
    "document_head", "document_compare", "cache_read", "cache_write", "busy",
}


def mark(phase, *, http_status=None, cache=None):
    event = _CURRENT.get()
    if event is None or phase not in _PHASES:
        return
    if phase in {"catalog_request", "metalink_request", "document_head"}:
        event.pop("http_status", None)
    event["phase"] = phase
    if type(http_status) is int and 100 <= http_status <= 599:
        event["http_status"] = http_status
    if cache in ("hit", "miss"):
        event["cache"] = cache


@contextmanager
def attempt(source_id, discovery, *, trigger):
    """Emit one event containing only validated source and check metadata."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", source_id):
        yield {}
        return
    event = {"request_id": uuid4().hex, "source_id": source_id, "trigger": trigger,
             "phase": "installed_identity", "cache": "not_used"}
    host = urlsplit(discovery["endpoint"]).hostname
    if host and re.fullmatch(r"(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}", host) and len(host) <= 253:
        event["publisher_host"] = host
    token = _CURRENT.set(event)
    start = time.monotonic()
    outcome = {}
    try:
        yield outcome
    finally:
        _CURRENT.reset(token)
        state = outcome.get("state")
        if not isinstance(state, str) or state not in update_check_store.STATES:
            state = "unable_to_determine"
        stamp = time.time()
        cached_at = outcome.get("checked_at")
        if event["cache"] == "hit" and type(cached_at) in (int, float) and 0 < cached_at <= stamp:
            event["cache_age_seconds"] = int(stamp - cached_at)
        event.update(state=state, checked_at=stamp,
                     reason_code=event["phase"] + "_failed" if state == "unable_to_determine" else state,
                     duration_ms=round((time.monotonic() - start) * 1000))
        LOGGER.log(logging.WARNING if state == "unable_to_determine" else logging.INFO,
                   "source_update_check %s", json.dumps(event, separators=(",", ":"), sort_keys=True))
