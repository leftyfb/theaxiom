"""Safe, explicit Kiwix edition discovery without archive acquisition.

Uses the official fixed OPDS endpoint and only Metalink references published
there. A successful catalog fetch does not mean an archive is installed,
downloaded, or authorized for automatic replacement.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from . import freshness_probe, kiwix_metadata_transport, kiwix_opds, update_diagnostics

CATALOG_URL = "https://opds.library.kiwix.org/catalog/v2/entries"
CHECK_LOCK = threading.Lock()
MIN_RECHECK_SECONDS = 300
CATALOG_PAGE_SIZE = 25
MAX_CATALOG_PAGES = 4
UNKNOWN = {"state": "unable_to_determine", "evidence": "publisher candidate could not be verified"}

# systemd captures stderr in the application journal. Keep this handler isolated
# from unrelated application logging, with no remote payloads or URLs.
LOGGER = logging.getLogger("theaxiom.kiwix_update")
if not LOGGER.handlers:
    _journal_handler = logging.StreamHandler()
    _journal_handler.setFormatter(logging.Formatter(
        "%(levelname)s theaxiom.update_check %(message)s"))
    LOGGER.addHandler(_journal_handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _report(result: dict, *, family: str, stage: str, **counts: int) -> dict:
    """Log validated archive identity and bounded diagnostic counts only."""
    safe_counts = " ".join(
        f"{name}={value}" for name, value in counts.items()
        if name in {"page", "entries", "parsed", "candidates"} and type(value) is int
    )
    level = logging.WARNING if result.get("state") == "unable_to_determine" else logging.INFO
    LOGGER.log(level, "family=%r stage=%s state=%s evidence=%s %s",
               family, stage, result.get("state"), result.get("evidence", "none"),
               safe_counts)
    return result


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
    LOGGER.info("family=%r stage=start edition=%s catalog_name=%r", family, edition, catalog_name)
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
            update_diagnostics.mark("catalog_request")
            raw = fetch(query, kind="catalog")
            stage = "catalog XML parsing"
            update_diagnostics.mark("catalog_parse")
            entries = kiwix_opds.parse_acquisition_feed(raw)
            stage = "catalog family and edition matching"
            # Count all Atom entries, including unsupported acquisition types,
            # so a partial page cannot silently terminate the catalog scan.
            update_diagnostics.mark("catalog_filter")
            entry_count = len(ET.fromstring(raw).findall(kiwix_opds.ATOM + "entry"))
            LOGGER.info("family=%r stage=catalog_scan page=%d entries=%d parsed=%d",
                        family, page, entry_count, len(entries))
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
            return _report(dict(UNKNOWN, evidence="filtered catalog scan exceeded the bounded page limit"),
                           family=family, stage="catalog_scan", candidates=len(candidates))
        if not candidates:
            # Listing the installed edition on a complete filtered scan is
            # evidence that no later edition was listed, not that the remote
            # artifact matches the installed digest.
            if installed_edition_listed:
                return _report({"state": "no_newer_edition_listed",
                        "evidence": "complete filtered catalog listed no later matching edition",
                        "installed_edition": edition}, family=family, stage="catalog_match")
            return _report(dict(UNKNOWN, evidence="installed edition was absent from the filtered publisher catalog"),
                           family=family, stage="catalog_match", candidates=0)
        update_diagnostics.mark("candidate_selection")
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        newest = candidates[0][0]
        # Ambiguous duplicate records must not result in an arbitrary choice.
        if sum(version == newest for version, _ in candidates) != 1:
            return _report(dict(UNKNOWN, evidence="multiple matching newest publisher editions were listed"),
                           family=family, stage="catalog_match", candidates=len(candidates))
        entry = candidates[0][1]
        link = entry["acquisition_url"]
        name = urlsplit(link).path.rsplit("/", 1)[-1][:-6]
        stage = "Metalink metadata request"
        update_diagnostics.mark("metalink_request")
        raw_metalink = fetch(link, kind="metalink")
        stage = "Metalink validation"
        update_diagnostics.mark("metalink_parse")
        metadata = kiwix_opds.parse_metalink(
            raw_metalink, expected_filename=name,
            preferred_host=urlsplit(installed["source_url"]).hostname,
        )
        stage = "publisher candidate identity comparison"
        update_diagnostics.mark("edition_comparison")
        candidate = kiwix_opds.resolve_catalog_candidate(entry, metadata, installed=installed)
        return _report(freshness_probe.compare_catalog_editions(
            installed, candidate, trusted_catalog=True,
        ), family=family, stage="edition_comparison", candidates=len(candidates))
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        # Exception type only; messages may include remote URLs or payloads.
        LOGGER.warning("family=%r stage=%s exception_type=%s",
                       family, stage, type(exc).__name__)
        return _report(dict(UNKNOWN, evidence=f"Kiwix metadata check failed during {stage}"),
                       family=family, stage=stage)


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
            # A checksum is optional at acquisition time. The completed,
            # independently verified artifact contains its measured SHA-256;
            # if the operator supplied an expected digest, it must match.
            or config.get("sha256") not in ("", digest)):
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
    """Check a verified ZIM, logging cache hits and fail-closed exits."""
    from . import update_check_store, zim_jobs
    # The source ID is generated by the validated local catalog.
    LOGGER.info("source_id=%r stage=request", source_id)
    if not CHECK_LOCK.acquire(blocking=False):
        update_diagnostics.mark("busy")
        LOGGER.warning("source_id=%r stage=lock state=unable_to_determine", source_id)
        return dict(UNKNOWN)
    try:
        job = zim_jobs.status(source_id)
        if not job:
            LOGGER.warning("source_id=%r stage=job_lookup state=unable_to_determine", source_id)
            return dict(UNKNOWN)
        artifact = zim_jobs.installed(job, job["configuration"])
        installed = installed_identity_from_job(job, artifact) if artifact else None
        if installed is None:
            LOGGER.warning("source_id=%r stage=installed_identity state=unable_to_determine", source_id)
            return dict(UNKNOWN)
        update_diagnostics.mark("cache_read", cache="miss")
        cached = update_check_store.load(source_id, installed["sha256"])
        if ("checked_at" in cached and
                0 <= time.time() - cached["checked_at"] < MIN_RECHECK_SECONDS):
            LOGGER.info("source_id=%r stage=cache_hit state=%s", source_id, cached["state"])
            update_diagnostics.mark("cache_read", cache="hit")
            return cached
        LOGGER.info("source_id=%r stage=metadata_check cache=miss", source_id)
        result = check_kiwix_update(installed, fetch=fetch)
        try:
            update_check_store.save(source_id, installed["sha256"], result)
        except (OSError, ValueError):
            update_diagnostics.mark("cache_write")
            raise
        LOGGER.info("source_id=%r stage=cache_save state=%s", source_id, result["state"])
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOGGER.warning("source_id=%r stage=wrapper_failure exception_type=%s",
                       source_id, type(exc).__name__)
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


def pinned_seed_identity(record: dict, artifact: dict, *, verified: bool) -> dict | None:
    """Match a verified legacy artifact against an immutable pinned ZIM seed.

    Works for any safely pinned Kiwix ZIM, never solely because a source ID,
    local filename or editable profile claims a matching edition. No archive
    reads, network calls or persistent state mutation occur here.
    """
    from . import source_catalog
    if (verified is not True or not isinstance(record, dict)
            or not isinstance(artifact, dict)
            or not isinstance(record.get("configuration"), dict)):
        return None
    config = record["configuration"]
    if config.get("format") != "zim":
        return None
    for seed in source_catalog.builtin_sources():
        if seed.get("kind") != "zim" or record.get("source_id") != seed.get("source_id"):
            continue
        if (not isinstance(seed.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", seed["sha256"])
                or type(seed.get("bytes")) is not int or seed["bytes"] < 96
                or not isinstance(seed.get("filename"), str)
                or any(artifact.get(key) != seed.get(key)
                       for key in ("filename", "source_url", "bytes", "sha256"))
                or config.get("source_url") != seed.get("source_url")
                or config.get("sha256") != seed["sha256"]
                or config.get("expected_size_bytes") != str(seed["bytes"])):
            return None
        name = seed["filename"]
        match = re.fullmatch(
            r"([a-z0-9._-]{3,120})_(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.zim", name)
        if not match or ".." in match.group(1):
            return None
        family, edition = match.groups()
        parts = family.split("_")
        language = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu"}.get(
            parts[1] if len(parts) >= 3 else "")
        if language is None or seed.get("language") != language:
            return None
        url = seed["source_url"]
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in kiwix_opds.DOWNLOAD_HOSTS
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.port not in (None, 443)):
                return None
        except (TypeError, ValueError):
            return None
        return dict(verified=True, sha256=seed["sha256"],
                    family=family, edition=edition, source_url=url,
                    language=language, variant=parts[-1], publisher="kiwix")
    return None


def pinned_wikipedia_identity(record: dict, artifact: dict, *, verified: bool) -> dict | None:
    """Backward-compatible alias for previously exported legacy pin checks."""
    return pinned_seed_identity(record, artifact, verified=verified)

def refresh_pinned_seed(record: dict, artifact: dict, *, verified: bool,
                             fetch=None) -> dict:
    """Check any trusted pinned seed without archive or download access."""
    from . import update_check_store
    sid = record.get("source_id") if isinstance(record, dict) else "<invalid>"
    LOGGER.info("source_id=%r stage=request pinned=true", sid)
    installed = pinned_seed_identity(record, artifact, verified=verified)
    if installed is None:
        LOGGER.warning("source_id=%r stage=installed_identity state=unable_to_determine", sid)
        return dict(UNKNOWN)
    if not CHECK_LOCK.acquire(blocking=False):
        update_diagnostics.mark("busy")
        LOGGER.warning("source_id=%r stage=lock state=unable_to_determine", sid)
        return dict(UNKNOWN)
    try:
        update_diagnostics.mark("cache_read", cache="miss")
        cached = update_check_store.load(sid, installed["sha256"])
        if ("checked_at" in cached
                and 0 <= time.time() - cached["checked_at"] < MIN_RECHECK_SECONDS):
            LOGGER.info("source_id=%r stage=cache_hit state=%s", sid, cached["state"])
            update_diagnostics.mark("cache_read", cache="hit")
            return cached
        LOGGER.info("source_id=%r stage=metadata_check cache=miss", sid)
        result = check_kiwix_update(installed, fetch=fetch)
        try:
            update_check_store.save(sid, installed["sha256"], result)
        except (OSError, ValueError):
            update_diagnostics.mark("cache_write")
            raise
        LOGGER.info("source_id=%r stage=cache_save state=%s", sid, result["state"])
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOGGER.warning("source_id=%r stage=wrapper_failure exception_type=%s",
                       sid, type(exc).__name__)
        return dict(UNKNOWN)
    finally:
        CHECK_LOCK.release()


def refresh_pinned_wikipedia(record: dict, artifact: dict, *, verified: bool,
                             fetch=None) -> dict:
    """Compatibility alias for callers migrating to format-driven checks."""
    return refresh_pinned_seed(record, artifact, verified=verified, fetch=fetch)
