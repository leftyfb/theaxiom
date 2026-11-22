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
