"""Deterministic end-to-end update discovery tests: zero real network or archives."""
from theaxiom import kiwix_update_discovery as discovery

FAMILY = "wikipedia_en_all_maxi"
ROOT = "https://download.kiwix.org/zim/wikipedia/"
INSTALLED = {
    "verified": True, "sha256": "a" * 64, "edition": "2026-08",
    "family": FAMILY, "language": "eng", "variant": "maxi",
    "publisher": "kiwix", "source_url": ROOT + FAMILY + "_2026-08.zim",
}


def atom(version, *, family=FAMILY):
    filename = f"{family}_{version}.zim"
    return (f'<entry><id>urn:uuid:{version}-{family}</id><title>English Wikipedia</title>'
            f'<link rel="http://opds-spec.org/acquisition/open-access" '
            f'type="application/x-zim" href="{ROOT}{filename}.meta4"/>'
            f'</entry>')


def feed(*entries):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">'
            + ''.join(entries) + '</feed>').encode()


def metalink(version, digest="b" * 64, *, family=FAMILY):
    filename = f"{family}_{version}.zim"
    return (f'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
            f'<file name="{filename}"><size>123456</size>'
            f'<hash type="sha-256">{digest}</hash>'
            f'<url>{ROOT}{filename}</url></file></metalink>').encode()


def fetcher(catalog, metadata, calls):
    def fetch(url, *, kind):
        calls.append((kind, url))
        if kind == "catalog":
            assert url == discovery.CATALOG_URL
            return catalog
        assert kind == "metalink"
        return metadata
    return fetch


def test_newer_edition_requires_both_documents_and_sha():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED,
        fetch=fetcher(feed(atom("2026-11"), atom("2026-09")),
                      metalink("2026-11"), calls),
    )
    assert result["state"] == "update_available"
    assert result["candidate_edition"] == "2026-11"
    assert result["candidate_sha256"] == "b" * 64
    assert [kind for kind, _ in calls] == ["catalog", "metalink"]


def test_no_newer_entry_does_not_assume_up_to_date():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(atom("2026-08")), b"", calls))
    assert result["state"] == "unable_to_determine"
    assert len(calls) == 1


def test_duplicate_latest_editions_are_ambiguous_without_metalink_request():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED,
        fetch=fetcher(feed(atom("2026-11"), atom("2026-11")),
                      metalink("2026-11"), calls),
    )
    assert result["state"] == "unable_to_determine"
    assert len(calls) == 1


def test_metalink_mismatch_does_not_report_update():
    for doc in (metalink("2026-10"), metalink("2026-11", "a" * 64),
                b"<html/>", b""):
        calls = []
        result = discovery.check_kiwix_update(
            INSTALLED, fetch=fetcher(feed(atom("2026-11")), doc, calls))
        assert result["state"] == "unable_to_determine"
        assert len(calls) == 2


def test_untrusted_installation_never_checks_network():
    for bad in ({"verified": False}, {"sha256": "bad"},
                {"edition": "not-a-date"}, {"publisher": "unknown"},
                {"family": "../etc"}, {"language": ""}):
        def forbidden(*args, **kwargs):
            raise AssertionError("Network must not be consulted")
        result = discovery.check_kiwix_update(
            dict(INSTALLED, **bad), fetch=forbidden)
        assert result["state"] == "unable_to_determine"


def test_invalid_or_unavailable_catalog_fails_closed():
    for doc in (b"", b"invalid", b"<feed/>"):
        calls = []
        result = discovery.check_kiwix_update(
            INSTALLED, fetch=fetcher(doc, metalink("2026-11"), calls))
        assert result["state"] == "unable_to_determine"
        assert len(calls) == 1


def test_fetch_failure_keeps_status_unknown():
    def offline(*args, **kwargs):
        raise OSError("offline")
    assert discovery.check_kiwix_update(INSTALLED, fetch=offline)["state"] == "unable_to_determine"


def test_different_family_is_not_a_candidate():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED,
        fetch=fetcher(feed(atom("2026-12", family="wikipedia_en_all_mini")),
                      metalink("2026-12"), calls),
    )
    assert result["state"] == "unable_to_determine"
    assert len(calls) == 1


def installed_job_for_fixture():
    digest = "a" * 64
    return {
        "source_id": "wikipedia", "state": "completed",
        "verified": True, "active": False,
        "configuration": {
            "format": "zim",
            "source_url": ROOT + FAMILY + "_2026-08.zim",
            "sha256": digest,
        },
        "artifact": {
            "filename": "wikipedia." + digest + ".zim",
            "sha256": digest, "bytes": 123456,
        },
    }


def test_verified_job_yields_installed_edition_without_catalog_profile():
    job = installed_job_for_fixture()
    result = discovery.installed_identity_from_job(job, job["artifact"])
    assert result == INSTALLED


def test_verified_job_refuses_mismatched_identity_or_unverified_state():
    job = installed_job_for_fixture()
    modifications = [
        {"verified": False}, {"active": True}, {"state": "failed"},
        {"configuration": dict(job["configuration"], source_url="https://evil.invalid/test.zim")},
        {"configuration": dict(job["configuration"], sha256="b" * 64)},
        {"artifact": dict(job["artifact"], filename="wrong.zim")},
    ]
    for changes in modifications:
        corrupted = dict(job, **changes)
        assert discovery.installed_identity_from_job(
            corrupted, corrupted["artifact"]) is None


def test_job_wrapper_only_checks_network_after_real_install_validation(monkeypatch):
    from theaxiom import zim_jobs
    job = installed_job_for_fixture()
    monkeypatch.setattr(zim_jobs, "status", lambda source_id: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda j, c: j["artifact"])
    calls = []
    result = discovery.check_installed_kiwix_job(
        "wikipedia", fetch=fetcher(feed(atom("2026-11")),
                                   metalink("2026-11"), calls))
    assert result["state"] == "update_available"
    assert len(calls) == 2

    monkeypatch.setattr(zim_jobs, "installed", lambda j, c: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("Network must not run when installed bytes are unverified")
    assert discovery.check_installed_kiwix_job(
        "wikipedia", fetch=forbidden)["state"] == "unable_to_determine"


def test_ready_gov_dotted_archive_family_is_recognized():
    job = installed_job_for_fixture()
    url = "https://download.kiwix.org/zim/zimit/ready.gov_en_all_2026-10.zim"
    job["configuration"] = dict(job["configuration"], source_url=url)
    parsed = discovery.installed_identity_from_job(job, job["artifact"])
    assert parsed["family"] == "ready.gov_en_all"
    assert parsed["edition"] == "2026-10"


def test_explicit_check_persists_only_for_current_verified_install(monkeypatch, tmp_path):
    from theaxiom import zim_jobs, update_check_store
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    job = installed_job_for_fixture()
    monkeypatch.setattr(zim_jobs, "status", lambda sid: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda live, config: live["artifact"])
    calls = []
    result = discovery.refresh_checked_source(
        "wikipedia", fetch=fetcher(feed(atom("2026-11")), metalink("2026-11"), calls))
    assert result["state"] == "update_available"
    assert len(calls) == 2
    cached = discovery.cached_checked_source("wikipedia")
    assert cached["state"] == "update_available"
    assert cached["candidate_edition"] == "2026-11"

    # A different installed revision must not inherit the older check.
    changed = dict(job)
    digest = "c" * 64
    changed["configuration"] = dict(job["configuration"], sha256=digest)
    changed["artifact"] = dict(job["artifact"], sha256=digest,
                               filename="wikipedia." + digest + ".zim")
    monkeypatch.setattr(zim_jobs, "status", lambda sid: changed)
    assert discovery.cached_checked_source("wikipedia")["state"] == "unable_to_determine"


def test_unverified_installed_source_never_writes_update_cache(monkeypatch, tmp_path):
    from theaxiom import zim_jobs, update_check_store
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    job = installed_job_for_fixture()
    job["verified"] = False
    monkeypatch.setattr(zim_jobs, "status", lambda sid: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda live, config: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("Network must not be contacted for unverified content")
    assert discovery.refresh_checked_source(
        "wikipedia", fetch=forbidden)["state"] == "unable_to_determine"
    assert not (tmp_path / "checks").exists()
