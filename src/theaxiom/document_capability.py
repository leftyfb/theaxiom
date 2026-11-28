"""Advisory, exact-URL document discovery capability; never installs content."""
import hashlib
import json
import os

from . import (document_origin_evidence, document_replacement, pdf_source,
               pdf_transport, update_check_store, update_discovery_config)


def result(state, reason, evidence):
    return dict(state=state, reason=reason, evidence=evidence,
                recommended_policy={"supported": "notify", "unsupported": "disabled"}.get(state, ""))


def identity(config):
    selected = update_discovery_config.settings(config)
    return hashlib.sha256(json.dumps([config.get("format"), config.get("source_url"),
                                      selected["method"], selected["endpoint"],
                                      config.get("sha256", "")], separators=(",", ":")).encode()).hexdigest()


def status(record):
    config = record["configuration"]
    if config.get("format") not in {"pdf", "epub"}:
        return None
    if not config.get("source_url"):
        return result("unsupported", "local_only", "Local-only document; no publisher URL to compare.")
    try:
        selected = update_discovery_config.settings(config)
        if selected["method"] != "head" or selected["endpoint"] != config.get("source_url"):
            return result("unsupported", "unsupported_url", "Comparable discovery requires the exact configured HTTPS download URL.")
        target = update_check_store.record_path(record["source_id"]).with_suffix(".capability.json")
        raw = update_check_store.read_bytes(target)
        if raw:
            data = json.loads(raw)
            outcome = data["result"]
            if (data["identity"] == identity(config)
                    and outcome["state"] in {"supported", "unsupported", "unknown", "pending"}
                    and outcome["recommended_policy"] in {"", "notify", "disabled"}):
                return outcome
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return result("pending", "not_checked", "Capability not established for this exact URL and installed file.")


def save(record, outcome):
    target = update_check_store.record_path(record["source_id"]).with_suffix(".capability.json")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(dict(identity=identity(record["configuration"]), result=outcome)).encode()
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        update_check_store._write(target, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect(record, *, installed_etag=None, pin=None, establish=False, probe=None):
    """Compare publisher metadata with an actual GET validator.

    Explicit baseline recovery reads at most the installed size, checks its
    digest, and requires the reviewed strong ETag at the same exact URL.
    """
    config = record["configuration"]
    url = config.get("source_url", "")
    selected = update_discovery_config.settings(config)
    if not url:
        return result("unsupported", "local_only", "Local-only document; no publisher URL to compare.")
    if not pdf_source.valid_source_url(url) or selected["method"] != "head" or selected["endpoint"] != url:
        return result("unsupported", "unsupported_url", "Comparable discovery requires the exact configured HTTPS download URL.")
    if pin and pin.get("source_url") != url:
        return result("unsupported", "changed_url", "Installed document identity differs from the configured URL.")
    if pin and installed_etag is None:
        installed_etag = document_replacement.validator(record, pin)
    if not isinstance(installed_etag, str) or not document_origin_evidence.ETAG.fullmatch(installed_etag):
        installed_etag = None
    probe = probe or pdf_transport.probe_document_metadata
    try:
        response = probe(url, if_none_match=installed_etag) if installed_etag else probe(url)
    except (OSError, ValueError):
        return result("unknown", "temporary_or_unavailable", "Publisher metadata request failed or timed out; policy unchanged.")
    code = response.get("status")
    if not response.get("reachable"):
        if code in {401, 403}:
            return result("unknown", "access_denied", f"Publisher denied metadata access (HTTP {code}); policy unchanged.")
        if code in {405, 501}:
            return result("unsupported", "unsupported_method", f"Publisher does not support comparable metadata requests (HTTP {code}).")
        if code in {301, 302, 303, 307, 308}:
            return result("unsupported", "redirect", "Publisher redirected the exact URL; comparable discovery is not established.")
        return result("unknown", "temporary_or_unavailable", "Publisher metadata unavailable; policy unchanged.")
    if response.get("source_url", url) != url:
        return result("unsupported", "changed_url", "Metadata refers to a different URL; comparison refused.")
    tag = response.get("etag")
    if not isinstance(tag, str) or not document_origin_evidence.ETAG.fullmatch(tag):
        tag = None
    if installed_etag and (code == 304 or tag):
        return result("supported", "comparable", "Strong capture-time ETag can be compared at the exact URL. This does not prove future editions will be available.")
    if not tag:
        weak = response.get("validator_kind") == "weak"
        return result("unsupported", "weak_validator" if weak else "missing_validator",
                      "Publisher returned only a weak ETag." if weak else "Publisher supplied no comparable strong ETag.")
    if not pin:
        return result("pending", "awaiting_download", "Publisher exposes a strong ETag; a verified GET is required before enabling notifications.")
    if not establish:
        return result("unsupported", "no_capture_validator", "No strong ETag was captured for the installed bytes; recheck capability to attempt a verified baseline.")
    try:
        digest = hashlib.sha256()
        count = 0
        with pdf_transport.open_pdf(url, format_name=config["format"], limit=pin["bytes"],
                                    total_seconds=15, required_etag=tag) as stream:
            while block := stream.read(64 * 1024):
                count += len(block)
                if count > pin["bytes"]:
                    raise ValueError("Document exceeds installed size")
                digest.update(block)
            if (count != pin["bytes"] or digest.hexdigest() != pin["sha256"]
                    or stream.etag != tag):
                return result("unknown", "baseline_mismatch", "Publisher bytes or validator differ from the installed document; baseline not changed.")
        document_origin_evidence.save(record["source_id"], config["format"], url, pin["sha256"], tag)
        return result("supported", "baseline_verified", "Re-fetched bytes match the installed SHA-256 and strong ETag; baseline established without replacing content. Future editions are not guaranteed.")
    except (OSError, ValueError):
        return result("unknown", "baseline_unavailable", "Bounded baseline verification failed; installed content and policy unchanged.")
