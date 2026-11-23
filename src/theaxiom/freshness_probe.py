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
        "etag": "",
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
            result["etag"] = response.headers.get("ETag", "")
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
                "last_modified": "", "etag": "", "content_length": "",
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
    # Missing identities must never compare equal to missing job fields.
    # A verified checksum without an unambiguous source/file binding is not
    # evidence that the requested local artifact is installed.
    source_id = entry.get("source_id")
    filename = entry.get("filename")
    if (not isinstance(source_id, str) or not source_id.strip()
            or not isinstance(filename, str) or not filename.strip()):
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


def classify_remote_revision(
    installed: dict[str, Any] | None,
    remote: dict[str, Any] | None,
) -> dict[str, str]:
    """Compare a verified installed artifact with authoritative revision evidence.

    A changed Content-Length, Last-Modified, weak ETag, or candidate URL is not
    proof of a newer edition. Strong HTTP entity tags can identify a changed
    *representation* only when the URL is unchanged; this is not itself an
    "update available" assertion. A publisher catalog can assert a newer
    edition only with an explicit, comparable monotonic version and a pinned
    candidate digest. No I/O, downloads, or mutation occurs here.
    """
    unknown = {"state": "unable_to_determine", "evidence": "insufficient trusted revision evidence"}
    if not isinstance(installed, dict) or not isinstance(remote, dict):
        return unknown
    if installed.get("verified") is not True:
        return unknown
    digest = installed.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        return unknown

    def strong_etag(value):
        return (isinstance(value, str) and len(value) >= 3
                and len(value) <= 256 and value.startswith('"')
                and value.endswith('"') and not value.startswith('W/')
                and all(ord(char) >= 32 and ord(char) != 127 for char in value))

    source_url = installed.get("source_url")
    if (isinstance(source_url, str) and source_url.startswith("https://")
            and remote.get("source_url") == source_url
            and strong_etag(installed.get("etag"))
            and strong_etag(remote.get("etag"))):
        if installed["etag"] == remote["etag"]:
            return {"state": "representation_unchanged", "evidence": "matching strong ETag"}
        return {"state": "representation_changed", "evidence": "different strong ETag; newer edition unproven"}

    return unknown


def compare_catalog_editions(
    installed: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    trusted_catalog: bool = False,
) -> dict[str, Any]:
    """Compare edition metadata supplied by a separately verified publisher resolver.

    This is a pure decision function, not a URL resolver or security boundary.
    The caller MUST authenticate an allowlisted publisher catalog and bind its
    entries to the installed source family before passing trusted_catalog=True.
    Arbitrary scraped listings or values sent by a browser are not authority.
    """
    unknown: dict[str, Any] = {
        "state": "unable_to_determine",
        "evidence": "no authenticated publisher edition comparison",
    }
    if trusted_catalog is not True or not isinstance(installed, dict) or not isinstance(candidate, dict):
        return unknown

    # Identity fields come from verified installed metadata and authenticated
    # publisher catalog entries, NOT filename guessing or remote byte length.
    from datetime import date
    from urllib.parse import urlsplit

    def valid_digest(value):
        return (isinstance(value, str) and len(value) == 64
                and all(char in "0123456789abcdef" for char in value))

    def edition_date(value):
        if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
            return None
        try:
            year, month = int(value[:4]), int(value[5:])
            return date(year, month, 1) if value[:4].isdigit() and value[5:].isdigit() else None
        except ValueError:
            return None

    def https_location(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
                return None
            return parsed.hostname.lower()
        except ValueError:
            return None

    if (installed.get("verified") is not True
            or not valid_digest(installed.get("sha256"))
            or not valid_digest(candidate.get("sha256"))
            or not isinstance(installed.get("family"), str)
            or not installed["family"]
            or candidate.get("family") != installed["family"]
            or not isinstance(installed.get("language"), str)
            or not installed["language"]
            or candidate.get("language") != installed["language"]
            or candidate.get("variant") != installed.get("variant")
            or candidate.get("publisher") != installed.get("publisher")
            or not isinstance(installed.get("publisher"), str)
            or not installed["publisher"]):
        return unknown
    installed_host = https_location(installed.get("source_url"))
    candidate_host = https_location(candidate.get("source_url"))
    if not installed_host or candidate_host != installed_host:
        return unknown
    old_edition = edition_date(installed.get("edition"))
    new_edition = edition_date(candidate.get("edition"))
    if not old_edition or not new_edition:
        return unknown
    if new_edition > old_edition and candidate["sha256"] != installed["sha256"]:
        result = {
            "state": "update_available",
            "evidence": "newer authenticated publisher catalog edition with distinct SHA-256",
            "installed_edition": installed["edition"],
            "candidate_edition": candidate["edition"],
            "candidate_url": candidate["source_url"],
            "candidate_sha256": candidate["sha256"],
        }
        if type(candidate.get("bytes")) is int and 96 <= candidate["bytes"] <= 2**63 - 1:
            result["candidate_bytes"] = candidate["bytes"]
        return result
    if new_edition == old_edition and candidate["sha256"] == installed["sha256"]:
        return {"state": "up_to_date", "evidence": "same authenticated catalog edition and SHA-256"}
    if new_edition < old_edition:
        return {"state": "older_candidate", "evidence": "publisher catalog edition predates installed edition"}
    return unknown


def classify_document_origin(
    installed: dict[str, Any] | None,
    response: dict[str, Any] | None,
) -> dict[str, str]:
    """Interpret a guarded metadata result against installed *capture-time* evidence.

    Only the strong ETag recorded with the already verified installed
    artifact is comparable. A changed ETag means representation changed,
    not that a newer licensed/verified edition exists. Size and timestamps
    are intentionally ignored for update availability.
    """
    unknown = {"state": "unable_to_determine",
               "evidence": "no comparable installed document validator"}
    if (not isinstance(installed, dict) or not isinstance(response, dict)
            or response.get("reachable") is not True
            or installed.get("verified") is not True
            or response.get("source_url") != installed.get("source_url")):
        return unknown
    import re
    old_tag = installed.get("etag")
    if (not isinstance(old_tag, str) or
            re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', old_tag) is None):
        return unknown
    if response.get("status") == 304:
        return {"state": "representation_unchanged",
                "evidence": "publisher returned HTTP 304 for installed strong ETag"}
    if response.get("status") not in (200, 206):
        return unknown
    return classify_remote_revision(installed, response)
