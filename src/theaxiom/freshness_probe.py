"""Reusable HTTP freshness-probe logic for the offline-library backend."""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Iterable


def probe_source(
    source_url: str,
    expected_sha256: str,
    expected_bytes: int,
    timeout: float = 5.0,
    opener=urllib.request.urlopen,
) -> dict[str, Any]:
    """Probe origin metadata only; a HEAD response cannot verify local bytes."""
    result: dict[str, Any] = {
        "source_url": source_url,
        "reachable": False,
        "last_modified": "",
        "content_length": "",
        "local_current": False,  # compatibility: never infer installed state from HEAD
        "origin_length_matches_manifest": None,
        "error": "",
    }
    request = urllib.request.Request(source_url, method="HEAD")
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            if not 200 <= status < 400:
                result["error"] = f"HTTP {status}"
                return result
            result["reachable"] = True
            result["last_modified"] = response.headers.get("Last-Modified", "")
            result["content_length"] = response.headers.get("Content-Length", "")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    reported_length = result["content_length"]
    if reported_length and expected_bytes:
        try:
            result["origin_length_matches_manifest"] = int(reported_length) == int(expected_bytes)
        except (ValueError, TypeError):
            result["origin_length_matches_manifest"] = None
    return result


def probe_documents(
    documents: Iterable[dict[str, Any]],
    timeout: float = 5.0,
    opener=urllib.request.urlopen,
) -> list[dict[str, Any]]:
    """Probe each manifest document and attach its identifier and title."""
    results = []
    for doc in documents:
        source_url = doc.get("source_url", "")
        if not source_url:
            results.append({
                "source_url": "", "id": doc.get("id", ""),
                "title": doc.get("title", ""), "reachable": False,
                "last_modified": "", "content_length": "",
                "local_current": False, "origin_length_matches_manifest": None,
                "error": "no source_url in manifest",
            })
            continue
        probe = probe_source(source_url, doc.get("sha256", ""), doc.get("bytes", 0), timeout, opener)
        probe.update(id=doc.get("id", ""), title=doc.get("title", ""))
        results.append(probe)
    return results


def installed_evidence(snapshot: dict[str, Any] | None, entry: dict[str, Any]) -> dict[str, Any]:
    """Classify trusted local acquisition evidence without reading archive bytes.

    A same-sized file is *not* verified by size. Only a fresh acquisition
    snapshot tied to the exact manifest identity and a completed verification
    can establish installed/current. Unknown storage remains unknown.
    """
    result: dict[str, Any] = {"local_current": False, "installed_state": "unknown",
                              "installed_verified": False}
    if not isinstance(snapshot, dict) or snapshot.get("stale") is not False:
        return result
    jobs = snapshot.get("acquisitions")
    if not isinstance(jobs, list):
        return result
    expected_bytes = entry.get("bytes")
    expected_digest = entry.get("sha256")
    if (type(expected_bytes) is not int or expected_bytes <= 0
            or not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(c not in "0123456789abcdef" for c in expected_digest)):
        return result
    matches = [job for job in jobs if isinstance(job, dict)
               and job.get("source_id") == entry.get("source_id")
               and job.get("filename") == entry.get("filename")
               and type(job.get("expected_bytes")) is int
               and job["expected_bytes"] == expected_bytes
               and job.get("sha256") == expected_digest]
    if len(matches) != 1:
        return result
    job = matches[0]
    if (job.get("state") == "completed" and job.get("verified") is True
            and job.get("active") is False
            and type(job.get("downloaded_bytes")) is int
            and job["downloaded_bytes"] == expected_bytes):
        return {"local_current": True, "installed_state": "verified",
                "installed_verified": True}
    state = job.get("state")
    if state == "not_started" and type(job.get("downloaded_bytes")) is int and job["downloaded_bytes"] == 0:
        result["installed_state"] = "absent"
    elif isinstance(state, str) and state in {"downloading", "interrupted", "verifying"}:
        result["installed_state"] = "partial_or_unverified"
    elif state == "unavailable":
        result["installed_state"] = "unavailable"
    elif state == "failed":
        result["installed_state"] = "failed_or_corrupt"
    return result
