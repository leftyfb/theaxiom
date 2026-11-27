"""Safe PDF/EPUB replacement using immutable, locally verified document pins.

The serving catalog is the atomic activation boundary. A replacement ledger
records both pins and capture-time validators before any serving pin changes.
HTTP validators identify representations at an approved URL, not editions or
publisher-authenticated checksums. All callers serialize mutations with the
existing publication and per-source worker locks.
"""
from hashlib import sha256
import json
import os
from uuid import uuid4

from . import (acquisition_queue, catalog_store, document_origin_evidence,
               epub_source, pdf_source, pdf_transport, update_check_store,
               update_discovery_config)

FORMATS = {"pdf": pdf_source, "epub": epub_source}


def _adapter(config):
    if config.get("format") not in FORMATS:
        raise ValueError("Not a supported document")
    return FORMATS[config["format"]]


def installed(record, *, verify=False):
    try:
        adapter = _adapter(record["configuration"])
        pin = adapter.pin(record["source_id"], record["configuration"])
        return pin if adapter.ready(pin, verify=verify) else None
    except (ValueError, OSError, KeyError, TypeError):
        return None


def identity(config):
    # Changing scheduling or search preferences does not invalidate a candidate.
    ignored = catalog_store.UPDATE_POLICY_FIELDS | {"sha256", "expected_size_bytes",
                                                     "update_periodic", "search_enabled"}
    return sha256(json.dumps({k: v for k, v in config.items() if k not in ignored},
                             sort_keys=True).encode()).hexdigest()


def _path(sid):
    return update_check_store.record_path(sid).with_suffix(".document-replacement.json")


def _etag(value):
    return isinstance(value, str) and document_origin_evidence.ETAG.fullmatch(value) is not None


def token(base_digest, url, etag, size):
    """A queue review token, deliberately not a publisher checksum."""
    return sha256(json.dumps([base_digest, url, etag, size], separators=(",", ":")).encode()).hexdigest()


def _fields(pin):
    return {"sha256": pin["sha256"], "expected_size_bytes": str(pin["bytes"])}


def _pin(record, item):
    config = record["configuration"]
    return _adapter(config).pin(record["source_id"], config | _fields(item))


def _validate(record, data):
    if not isinstance(data, dict) or set(data) != {
            "source_id", "identity", "base", "target", "job", "rollback_token"}:
        raise ValueError("Invalid document replacement record")
    if (data["source_id"] != record["source_id"]
            or not update_check_store._digest(data["identity"])
            or not isinstance(data["job"], str)
            or len(data["job"]) > 64
            or not isinstance(data["rollback_token"], str)
            or len(data["rollback_token"]) > 64):
        raise ValueError("Invalid document replacement identity")
    for key in ("base", "target"):
        item = data[key]
        if (not isinstance(item, dict) or set(item) != {"sha256", "bytes", "etag"}
                or not update_check_store._digest(item["sha256"])
                or type(item["bytes"]) is not int or not 0 < item["bytes"] <= pdf_source.MAX_BYTES
                or not _etag(item["etag"])):
            raise ValueError("Invalid document replacement pin")
    if data["base"]["sha256"] == data["target"]["sha256"]:
        raise ValueError("Document replacement pins overlap")
    return data


def read(record):
    path = _path(record["source_id"])
    raw = update_check_store.read_bytes(path)
    if raw is None:
        if path.exists() or path.is_symlink():
            raise ValueError("Document replacement ledger unavailable")
        return None
    try:
        return _validate(record, json.loads(raw))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid document replacement ledger") from exc


def _save(record, data):
    _validate(record, data)
    path = _path(record["source_id"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    if len(raw) > update_check_store.MAX_RECORD:
        raise ValueError("Document replacement ledger too large")
    update_check_store._write(path, raw)
    _sync(path)


def _sync(path):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def forget(sid):
    path = _path(sid)
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise ValueError("Unsafe document replacement ledger")
        path.unlink()
        _sync(path)


def status(record, data=None):
    data = read(record) if data is None else data
    if data is None:
        return None
    config = record["configuration"]
    if data["identity"] != identity(config):
        return {"state": "unavailable", "error": "Document configuration changed; review retained files"}
    active = config.get("sha256")
    if active == data["target"]["sha256"] and config.get("expected_size_bytes") == str(data["target"]["bytes"]):
        state = "activated"
    elif active == data["base"]["sha256"] and config.get("expected_size_bytes") == str(data["base"]["bytes"]):
        state = "rolled_back" if data["rollback_token"] else "verified"
    else:
        return {"state": "unavailable", "error": "Installed document no longer matches replacement"}
    # No full hashing on status polling. Activation/rollback reverify both files.
    adapter = _adapter(config)
    if not all(adapter.ready(_pin(record, data[key]), verify=False) for key in ("base", "target")):
        return {"state": "unavailable", "error": "Retained document missing or invalid"}
    return {"state": state, "sha256": data["target"]["sha256"],
            "expected_bytes": data["target"]["bytes"], "downloaded_bytes": data["target"]["bytes"]}


def validator(record, pin):
    """Recover exact GET evidence even if service stopped after catalog commit."""
    sid, config = record["source_id"], record["configuration"]
    saved = document_origin_evidence.load(sid, config["format"], pin["source_url"], pin["sha256"])
    if saved:
        return saved
    data = read(record)
    if data and identity(config) == data["identity"]:
        for key in ("base", "target"):
            item = data[key]
            if pin["sha256"] == item["sha256"] and pin["bytes"] == item["bytes"]:
                return item["etag"]
    return None


def evidence(record, result, response, old_etag, pin):
    """Attach actionable representation evidence only for a bounded exact URL.

    Preserve representation_changed, not update_available (which denotes a
    publisher-verified newer edition). A token is not a candidate digest.
    """
    config = record["configuration"]
    url = pin["source_url"]
    tag, size = response.get("etag"), response.get("content_length")
    if (result.get("state") == "representation_changed"
            and response.get("source_url") == url
            and response.get("reachable") is True and response.get("status") in (200, 206)
            and _etag(old_etag) and _etag(tag) and tag != old_etag
            and isinstance(size, str) and size.isascii() and size.isdecimal()
            and len(size) <= 10 and 0 < int(size) <= pdf_source.MAX_BYTES
            and update_discovery_config.settings(config)["endpoint"] == url):
        return dict(result, candidate_url=url, candidate_bytes=int(size),
                    candidate_etag=tag, candidate_token=token(pin["sha256"], url, tag, int(size)))
    return result


def checked(record, now=None):
    pin = installed(record)
    if not pin:
        return {"state": "unable_to_determine", "evidence": "No verified installed document"}
    config = record["configuration"]
    return update_check_store.load(record["source_id"],
        update_discovery_config.cache_identity(config, pin["sha256"]), now=now)


def reviewed(record, review_token, result):
    pin = installed(record)
    if not pin:
        raise ValueError("No verified installed document")
    tag, size = result.get("candidate_etag"), result.get("candidate_bytes")
    url = pin["source_url"]
    if (result.get("state") != "representation_changed" or result.get("stale")
            or not _etag(tag) or type(size) is not int or not 0 < size <= pdf_source.MAX_BYTES
            or result.get("candidate_url") != url
            or update_discovery_config.settings(record["configuration"])["endpoint"] != url
            or result.get("candidate_token") != review_token
            or token(pin["sha256"], url, tag, size) != review_token
            or not validator(record, pin) or validator(record, pin) == tag):
        raise ValueError("Document update evidence changed; check again")
    return pin, tag, size


def download(record, review_token, result, *, job="", stopped=None, on_retry=None):
    """Verify incoming bytes without mutating the serving catalog."""
    pin, tag, size = reviewed(record, review_token, result)
    sid, config = record["source_id"], record["configuration"]
    adapter = _adapter(config)
    prior = status(record)
    if prior and prior["state"] not in {"activated", "rolled_back"}:
        raise ValueError("Resolve the existing document replacement first")
    if not adapter.ready(pin, verify=True):
        raise ValueError("Serving document failed verification")
    old_tag = validator(record, pin)

    def transfer():
        with pdf_transport.open_pdf(pin["source_url"], format_name=config["format"],
                                    required_etag=tag) as stream:
            # Also check here, before installing any bytes.
            if stream.etag != tag:
                raise ValueError("Publisher validator changed")
            result = adapter.install_stream(sid, stream, expected_size=size, stop=stopped)
            return result[0] if config["format"] == "epub" else result

    artifact = pdf_transport.run_with_download_retries(transfer, stopped=stopped, on_retry=on_retry)
    if stopped and stopped():
        raise acquisition_queue.Cancelled()
    if artifact["sha256"] == pin["sha256"]:
        # Changed validator but identical document: refresh origin evidence,
        # do not claim an update or manufacture a rollback.
        document_origin_evidence.save(sid, config["format"], pin["source_url"], pin["sha256"], tag)
        update_check_store.require_recheck(sid)
        return None
    data = {"source_id": sid, "identity": identity(config),
            "base": {"sha256": pin["sha256"], "bytes": pin["bytes"], "etag": old_tag},
            "target": {"sha256": artifact["sha256"], "bytes": artifact["bytes"], "etag": tag},
            "job": job, "rollback_token": ""}
    _save(record, data)
    return data


def switch(catalog, record, revision, *, rollback=False, trigger="manual", job=None):
    """Atomically change only the serving pin, preserving publication settings."""
    data = read(record)
    current = status(record, data)
    expected = {"activated"} if rollback else {"verified", "rolled_back"}
    if not current or current["state"] not in expected:
        raise ValueError("No eligible verified document replacement")
    if not rollback and job is not None and data["job"] != job:
        raise ValueError("Document replacement is not owned by this attempt")
    adapter = _adapter(record["configuration"])
    if not all(adapter.ready(_pin(record, data[key]), verify=True) for key in ("base", "target")):
        raise ValueError("Document replacement failed verification")
    target = data["base" if rollback else "target"]
    if rollback:
        # Durable rollback token is written first. The active catalog pin
        # distinguishes an interrupted rollback from a completed rollback.
        data["rollback_token"] = uuid4().hex
        _save(record, data)
    sid, config = record["source_id"], record["configuration"]
    update_check_store.require_recheck(sid)
    updated = catalog.mutate("edit", sid, _fields(target), revision,
                             activity_action="rolled_back" if rollback else "updated",
                             activity_trigger=trigger)
    # The durable two-pin ledger is authoritative if this advisory write fails.
    try:
        document_origin_evidence.save(sid, config["format"], config["source_url"],
                                      target["sha256"], target["etag"])
    except (OSError, ValueError):
        pass
    return updated


def delete_staged(record, digest):
    data = read(record)
    state = status(record, data)
    if (not state or state["state"] not in {"verified", "rolled_back"}
            or data["target"]["sha256"] != digest):
        raise ValueError("Verified staged document changed")
    config, sid = record["configuration"], record["source_id"]
    adapter = _adapter(config)
    base, target = _pin(record, data["base"]), _pin(record, data["target"])
    if not adapter.ready(base, verify=True):
        raise ValueError("Serving document cannot be verified")
    # Save the old GET validator before retiring its recovery ledger.
    document_origin_evidence.save(sid, config["format"], base["source_url"],
                                  base["sha256"], data["base"]["etag"])
    update_check_store.require_recheck(sid)
    with pdf_source._directory("library", writing=True, format_name=config["format"]) as (directory, mount, _):
        with pdf_source._file(directory, mount, target["filename"], target["bytes"]) as stream:
            pdf_source.unlink_managed_file(directory, stream, target["filename"])
        os.fsync(directory)
    forget(sid)
