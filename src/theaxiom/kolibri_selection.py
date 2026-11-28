"""Bounded temporary channel metadata cache for explicit operator scope review."""
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
import threading
import time

from . import kolibri_bundle as bundle, kolibri_scope as scope

_LOCK = threading.Lock()
_CACHE = {}
TTL = 600
LIMIT = 2


def _prune():
    now = time.monotonic()
    for key, (directory, _, _, created) in list(_CACHE.items()):
        if now - created > TTL:
            _CACHE.pop(key)
            directory.cleanup()


@contextmanager
def metadata(channel_id, *, refresh=False):
    channel_id = scope._id(channel_id)
    with _LOCK:
        _prune()
        if refresh and channel_id in _CACHE:
            _CACHE.pop(channel_id)[0].cleanup()
        if channel_id not in _CACHE:
            while len(_CACHE) >= LIMIT:
                key = min(_CACHE, key=lambda k: _CACHE[k][3])
                _CACHE.pop(key)[0].cleanup()
            directory = tempfile.TemporaryDirectory(prefix="axiom-education-metadata-")
            try:
                latest = Path(directory.name) / "latest.sqlite3"
                channel, _ = bundle.fetch_metadata(channel_id, latest)
                path = Path(directory.name) / "channel.sqlite3"
                pinned, digest = bundle.fetch_metadata(channel_id, path, version=channel.version)
                if pinned != channel:
                    raise scope.ScopeError("Publisher channel changed while selecting its edition.")
                latest.unlink()
                _CACHE[channel_id] = (directory, channel, digest, time.monotonic())
            except BaseException:
                directory.cleanup()
                raise
        directory, channel, digest, _ = _CACHE[channel_id]
        yield Path(directory.name) / "channel.sqlite3", channel, digest


def review(config, data):
    channel_id = data.get("channel_id") or config.get("generator_channel")
    language = data.get("language") or config.get("generator_language", "eng")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{3}", language):
        raise scope.ScopeError("Enter a three-letter ISO 639-3 language code.")
    operation = data.get("operation")
    if operation not in {"browse", "preview", "save"}:
        raise scope.ScopeError("Invalid educational selection operation.")
    with metadata(channel_id, refresh=data.get("refresh") is True and operation == "browse") as (path, channel, digest):
        result = {"channel_id": channel.channel_id, "channel_name": channel.name,
                  "version": channel.version, "metadata_sha256": digest, "language": language}
        if operation == "browse":
            parent = data.get("parent_id") or channel.root_id
            result.update(parent_id=parent, root_id=channel.root_id,
                          topics=[{"id": n.node_id, "title": n.title, "kind": n.kind}
                                  for n in scope.children(path, channel_id, parent)])
            return result
        if data.get("metadata_sha256") != digest:
            raise scope.ScopeError("Channel metadata changed; browse and review again.")
        nodes = data.get("nodes")
        if (not isinstance(nodes, list) or not 1 <= len(nodes) <= bundle.MAX_SELECTION
                or any(not isinstance(node, str) or not scope.ID.fullmatch(node) for node in nodes)):
            raise scope.ScopeError("Select between one and fifteen topics or lessons.")
        nodes = list(dict.fromkeys(nodes))
        selected = bundle.select(path, channel_id, nodes, allow_full=False)
        result.update(transfer_bytes=selected.total_bytes,
                      estimated_peak_bytes=selected.total_bytes * 2 + bundle.MAX_METADATA,
                      lesson_count=len(selected.lessons), omitted=selected.omitted,
                      licenses=sorted({lesson.license for lesson in selected.lessons}),
                      nodes=nodes)
        if operation == "save":
            if data.get("confirmed") is not True or data.get("transfer_bytes") != selected.total_bytes:
                raise scope.ScopeError("Review the current size and licensing before saving.")
            result["fields"] = bundle.fields(selected, nodes, digest, language)
        return result
