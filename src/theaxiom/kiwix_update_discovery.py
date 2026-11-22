"""Safe, explicit Kiwix edition discovery without archive acquisition.

Uses the official fixed OPDS endpoint and only Metalink references published
there. A successful catalog fetch does not mean an archive is installed,
downloaded, or authorized for automatic replacement.
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from . import freshness_probe, kiwix_metadata_transport, kiwix_opds

CATALOG_URL = "https://opds.library.kiwix.org/catalog/v2/entries"
CHECK_LOCK = threading.Lock()
MIN_RECHECK_SECONDS = 300
CATALOG_PAGE_SIZE = 25
MAX_CATALOG_PAGES = 4
UNKNOWN = {"state": "unable_to_determine", "evidence": "publisher candidate could not be verified"}


def check_kiwix_update(installed: dict, *, fetch=None) -> dict:
    """Check one verified source using at most one catalog and one Metalink GET.

    The installed identity comes from a trusted verified job/manifest and is
    never built from the catalog or a user-edited source card. Only the
    transport's pinned TLS, checked DNS and allowlisted endpoints are trusted.
    """
    stage = "installed identity validation"
    if not isinstance(installed, dict) or installed.get("verified") is not True:
        return dict(UNKNOWN, evidence="installed identity is not independently verified")
    family = installed.get("family")
    edition = installed.get("edition")
    if (not isinstance(family, str) or not re.fullmatch(r"[a-z0-9._-]{3,120}", family)
            or not isinstance(edition, str)
            or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", edition)
            or installed.get("publisher") != "kiwix"
            or not isinstance(installed.get("language"), str)
            or not installed.get("language")
            or not isinstance(installed.get("source_url"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", str(installed.get("sha256", "")))):
        return dict(UNKNOWN, evidence="installed identity lacks verified catalog matching fields")
    # OPDS 'name' is the Kiwix book identity, not necessarily the complete
    # edition filename family. For known ZIM flavour suffixes, query the stable
    # book name and still require exact full-family candidate filenames below.
    catalog_name = family
    variant = installed.get("variant")
    if variant in {"maxi", "mini", "nopic"} and family.endswith("_" + variant):
        catalog_name = family[:-(len(variant) + 1)]
    # Caller-supplied fetch is only intended for deterministic test fixtures.
    fetch = kiwix_metadata_transport.fetch_metadata if fetch is None else fetch
    try:
        candidates = []
        installed_edition_listed = False
        completed_scan = False
        for page in range(MAX_CATALOG_PAGES):
            query = kiwix_metadata_transport.catalog_page_url(
                family=catalog_name, language=installed["language"],
                start=page * CATALOG_PAGE_SIZE, count=CATALOG_PAGE_SIZE,
            )
            stage = "catalog metadata request"
            raw = fetch(query, kind="catalog")
            stage = "catalog XML parsing"
            entries = kiwix_opds.parse_acquisition_feed(raw)
            stage = "catalog family and edition matching"
            # Count all Atom entries, including unsupported acquisition types,
            # so a partial page cannot silently terminate the catalog scan.
            entry_count = len(ET.fromstring(raw).findall(kiwix_opds.ATOM + "entry"))
            for entry in entries:
                if entry["acquisition_kind"] != "metalink":
                    continue
                path = urlsplit(entry["acquisition_url"]).path
                name = path.rsplit("/", 1)[-1]
                match = re.fullmatch(re.escape(family) + r"_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim\.meta4", name)
                if match and match.group(1) == edition:
                    installed_edition_listed = True
                if match and match.group(1) > edition:
                    candidates.append((match.group(1), entry))
            if entry_count < CATALOG_PAGE_SIZE:
                completed_scan = True
                break
        if not completed_scan:
            # The filtered catalog may have more pages: no claim that the
            # selected edition is the latest or unique without full evidence.
            return dict(UNKNOWN, evidence="filtered catalog scan exceeded the bounded page limit")
        if not candidates:
            # Listing the installed edition on a complete filtered scan is
            # evidence that no later edition was listed, not that the remote
            # artifact matches the installed digest.
            if installed_edition_listed:
                return {"state": "no_newer_edition_listed",
                        "evidence": "complete filtered catalog listed no later matching edition",
                        "installed_edition": edition}
            return dict(UNKNOWN, evidence="installed edition was absent from the filtered publisher catalog")
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        newest = candidates[0][0]
        # Ambiguous duplicate records must not result in an arbitrary choice.
        if sum(version == newest for version, _ in candidates) != 1:
            return dict(UNKNOWN, evidence="multiple matching newest publisher editions were listed")
        entry = candidates[0][1]
        link = entry["acquisition_url"]
        name = urlsplit(link).path.rsplit("/", 1)[-1][:-6]
        stage = "Metalink metadata request"
        raw_metalink = fetch(link, kind="metalink")
        stage = "Metalink validation"
        metadata = kiwix_opds.parse_metalink(
            raw_metalink, expected_filename=name,
            preferred_host=urlsplit(installed["source_url"]).hostname,
        )
        stage = "publisher candidate identity comparison"
        candidate = kiwix_opds.resolve_catalog_candidate(entry, metadata, installed=installed)
        return freshness_probe.compare_catalog_editions(
            installed, candidate, trusted_catalog=True,
        )
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        # Keep fail-closed semantics, but preserve a static, safe diagnostic
        # stage. Never echo exception text, untrusted URLs or remote payloads.
        return dict(UNKNOWN, evidence=f"Kiwix metadata check failed during {stage}")


def installed_identity_from_job(job: dict, artifact: dict) -> dict | None:
    """Derive edition identity from an independently validated installed ZIM.

    The caller must first obtain the job from the durable ledger and verify
    the archive with zim_jobs.installed(job, job["configuration"]). This
    function never turns the mutable source profile into installed evidence.
    """
    if (not isinstance(job, dict) or not isinstance(artifact, dict)
            or job.get("state") != "completed" or job.get("verified") is not True
            or job.get("active") is not False or job.get("artifact") != artifact):
        return None
    config = job.get("configuration")
    if not isinstance(config, dict) or config.get("format") != "zim":
        return None
    digest = artifact.get("sha256")
    source_id = job.get("source_id")
    if (not isinstance(source_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source_id)
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or artifact.get("filename") != f"{source_id}.{digest}.zim"
            or config.get("sha256") != digest):
        return None
    url = config.get("source_url")
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in kiwix_opds.DOWNLOAD_HOSTS
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.port not in (None, 443)):
            return None
    except ValueError:
        return None
    filename = parts.path.rsplit("/", 1)[-1]
    match = re.fullmatch(r"([a-z0-9._-]{3,120})_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim", filename)
    if not match or ".." in match.group(1):
        return None
    family, edition = match.groups()
    language_code = family.split("_")
    if len(language_code) < 3:
        return None
    # Only explicitly understood language tokens are supported; do not
    # guess arbitrary publisher naming conventions.
    language = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu"}.get(language_code[1])
    if language is None:
        return None
    variant = language_code[-1]
    return {
        "verified": True, "sha256": digest, "family": family,
        "edition": edition, "source_url": url,
        "language": language, "variant": variant, "publisher": "kiwix",
    }


def check_installed_kiwix_job(source_id: str, *, fetch=None) -> dict:
    """Discover updates only for a real, inactive, verified installed job."""
    from . import zim_jobs
    try:
        job = zim_jobs.status(source_id)
        if not job:
            return dict(UNKNOWN)
        artifact = zim_jobs.installed(job, job["configuration"])
        if not artifact:
            return dict(UNKNOWN)
        installed = installed_identity_from_job(job, artifact)
        if installed is None:
            return dict(UNKNOWN)
        return check_kiwix_update(installed, fetch=fetch)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)


def refresh_checked_source(source_id: str, *, fetch=None) -> dict:
    """Check a trusted installed ZIM with bounded, deduplicated metadata calls.

    A concurrent check fails closed, and even unsuccessful persisted checks are
    cached briefly to avoid creating a publisher retry storm.
    """
    from . import update_check_store, zim_jobs
    if not CHECK_LOCK.acquire(blocking=False):
        return dict(UNKNOWN)
    try:
        job = zim_jobs.status(source_id)
        if not job:
            return dict(UNKNOWN)
        artifact = zim_jobs.installed(job, job["configuration"])
        installed = installed_identity_from_job(job, artifact) if artifact else None
        if installed is None:
            return dict(UNKNOWN)
        cached = update_check_store.load(source_id, installed["sha256"])
        if ("checked_at" in cached and
                0 <= time.time() - cached["checked_at"] < MIN_RECHECK_SECONDS):
            return cached
        result = check_kiwix_update(installed, fetch=fetch)
        update_check_store.save(source_id, installed["sha256"], result)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)
    finally:
        CHECK_LOCK.release()


def cached_checked_source(source_id: str) -> dict:
    """Read current source evidence without network, using live installed identity."""
    from . import update_check_store, zim_jobs
    try:
        job = zim_jobs.status(source_id)
        if not job:
            return dict(UNKNOWN)
        artifact = zim_jobs.installed(job, job["configuration"])
        installed = installed_identity_from_job(job, artifact) if artifact else None
        if installed is None:
            return dict(UNKNOWN)
        return update_check_store.load(source_id, installed["sha256"])
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)


def pinned_wikipedia_identity(record: dict, artifact: dict, *, verified: bool) -> dict | None:
    """Resolve the legacy immutable Wikipedia ZIM only with verified evidence.

    A mutable source card or a public archive filename must never create an
    installed identity; the caller supplies independently matched monitoring
    evidence and the immutable shipped acquisition artifact.
    """
    from .source_catalog import WIKIPEDIA_SOURCE
    if (verified is not True or not isinstance(record, dict)
            or record.get("source_id") != WIKIPEDIA_SOURCE["source_id"]
            or not isinstance(record.get("configuration"), dict)
            or not isinstance(artifact, dict)
            or any(artifact.get(key) != WIKIPEDIA_SOURCE[key]
                   for key in ("filename", "source_url", "bytes", "sha256"))):
        return None
    config = record["configuration"]
    if (config.get("format") != "zim"
            or config.get("source_url") != WIKIPEDIA_SOURCE["source_url"]
            or config.get("sha256") != WIKIPEDIA_SOURCE["sha256"]
            or config.get("expected_size_bytes") != str(WIKIPEDIA_SOURCE["bytes"])):
        return None
    name = WIKIPEDIA_SOURCE["filename"]
    match = re.fullmatch(
        r"([a-z0-9._-]{3,120})_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim", name)
    if not match or ".." in match.group(1):
        return None
    family, edition = match.groups()
    return dict(verified=True, sha256=WIKIPEDIA_SOURCE["sha256"],
                family=family, edition=edition,
                source_url=WIKIPEDIA_SOURCE["source_url"], language="eng",
                variant="maxi", publisher="kiwix")


def refresh_pinned_wikipedia(record: dict, artifact: dict, *, verified: bool,
                             fetch=None) -> dict:
    """Check trusted pinned Wikipedia with no download or live archive access."""
    from . import update_check_store
    installed = pinned_wikipedia_identity(record, artifact, verified=verified)
    if installed is None:
        return dict(UNKNOWN)
    if not CHECK_LOCK.acquire(blocking=False):
        return dict(UNKNOWN)
    try:
        sid = record["source_id"]
        cached = update_check_store.load(sid, installed["sha256"])
        if ("checked_at" in cached
                and 0 <= time.time() - cached["checked_at"] < MIN_RECHECK_SECONDS):
            return cached
        result = check_kiwix_update(installed, fetch=fetch)
        update_check_store.save(sid, installed["sha256"], result)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return dict(UNKNOWN)
    finally:
        CHECK_LOCK.release()
