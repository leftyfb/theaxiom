"""Private capture-time HTTP validators for verified downloaded PDF/EPUB content.

A strong ETag is only evidence about the exact HTTPS representation that
was downloaded; it is not an independently authenticated publisher edition
or proof of a newer replacement. Never infer one from later HEAD responses.
"""
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import storage_settings, pdf_source

MAX_RECORD = 4096
ETAG = re.compile(r'"[\x21\x23-\x7e]{1,200}"')


def record_path(source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source_id):
        raise ValueError("Invalid document source ID")
    return storage_settings.SETTINGS_PATH.with_name("document-origin-validators") / (source_id + ".json")


def _validate(source_id, format_name, url, sha256, etag):
    record_path(source_id)
    if format_name not in {"pdf", "epub"}:
        raise ValueError("Unsupported document origin format")
    if not pdf_source.valid_source_url(url):
        raise ValueError("Invalid document origin URL")
    if not isinstance(sha256, str) or re.fullmatch(r"[a-f0-9]{64}", sha256) is None:
        raise ValueError("Invalid installed document digest")
    if not isinstance(etag, str) or ETAG.fullmatch(etag) is None:
        raise ValueError("Missing strong capture-time ETag")
    data = {"source_id": source_id, "format": format_name, "source_url": url,
            "sha256": sha256, "etag": etag}
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    if len(blob) > MAX_RECORD:
        raise ValueError("Oversized document origin evidence")
    return blob


def save(source_id, format_name, url, sha256, etag):
    """Record the ETag only after a verified document install and catalog commit."""
    data = _validate(source_id, format_name, url, sha256, etag)
    target = record_path(source_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError("Unsafe document origin record")
    fd, name = tempfile.mkstemp(prefix=".document-validator-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        folder = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        Path(name).unlink(missing_ok=True)


def load(source_id, format_name, url, sha256):
    """Return no validator unless the verified installed identity matches."""
    try:
        target = record_path(source_id)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > MAX_RECORD or info.st_mode & 0o077):
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                content = stream.read(MAX_RECORD + 1)
        finally:
            os.close(fd)
        data = json.loads(content)
        if (not isinstance(data, dict) or set(data) != {
                "source_id", "format", "source_url", "sha256", "etag"}
                or data["source_id"] != source_id or data["format"] != format_name
                or data["source_url"] != url or data["sha256"] != sha256):
            return None
        _validate(source_id, format_name, url, sha256, data["etag"])
        return data["etag"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
