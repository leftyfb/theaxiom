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
            assert url == discovery.kiwix_metadata_transport.catalog_page_url(
                family="wikipedia_en_all", language="eng", start=0, count=discovery.CATALOG_PAGE_SIZE
            )
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
    assert result["candidate_bytes"] == 123456
    assert [kind for kind, _ in calls] == ["catalog", "metalink"]


def test_catalog_filter_uses_book_name_but_candidate_matching_keeps_full_family():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED,
        fetch=fetcher(feed(atom("2026-11"), atom("2026-12", family="wikipedia_en_all_mini")),
                      metalink("2026-11"), calls),
    )
    assert result["state"] == "update_available"
    assert result["candidate_edition"] == "2026-11"
    assert "name=wikipedia_en_all&lang=eng" in calls[0][1]
    assert "name=wikipedia_en_all_maxi" not in calls[0][1]


def test_no_newer_entry_does_not_assume_up_to_date():
    calls = []
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(atom("2026-08")), b"", calls))
    assert result["state"] == "no_newer_edition_listed"
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


def test_unknown_update_results_explain_the_failing_stage_safely():
    def blocked_catalog(url, *, kind):
        raise OSError("secret network exception https://internal.invalid/token")

    outcome = discovery.check_kiwix_update(INSTALLED, fetch=blocked_catalog)
    assert outcome["state"] == "unable_to_determine"
    assert outcome["evidence"] == "Kiwix metadata check failed during catalog metadata request"
    assert "internal.invalid" not in str(outcome)

    absent = discovery.check_kiwix_update(INSTALLED, fetch=lambda url, *, kind: feed())
    assert absent["state"] == "unable_to_determine"
    assert "absent from the filtered" in absent["evidence"]

    bad_xml = discovery.check_kiwix_update(INSTALLED, fetch=lambda url, *, kind: b"invalid")
    assert bad_xml["state"] == "unable_to_determine"
    assert "catalog XML parsing" in bad_xml["evidence"]

    calls = []
    def bad_metalink(url, *, kind):
        calls.append(kind)
        if kind == "catalog":
            return feed(atom("2026-11"))
        raise ValueError("secret metadata error")

    metalink_error = discovery.check_kiwix_update(INSTALLED, fetch=bad_metalink)
    assert metalink_error["state"] == "unable_to_determine"
    assert "Metalink metadata request" in metalink_error["evidence"]
    assert "secret" not in str(metalink_error)
    assert calls == ["catalog", "metalink"]


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


def test_recent_manual_check_is_reused_without_recontacting_publisher(monkeypatch, tmp_path):
    from theaxiom import zim_jobs, update_check_store
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    job = installed_job_for_fixture()
    monkeypatch.setattr(zim_jobs, "status", lambda sid: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda live, config: live["artifact"])
    calls = []
    fetch = fetcher(feed(atom("2026-11")), metalink("2026-11"), calls)
    first = discovery.refresh_checked_source("wikipedia", fetch=fetch)
    assert first["state"] == "update_available"
    second = discovery.refresh_checked_source("wikipedia",
        fetch=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Recent result must avoid a network request")))
    assert second["state"] == "update_available"
    assert len(calls) == 2


def test_concurrent_check_does_not_start_another_remote_lookup(monkeypatch):
    assert discovery.CHECK_LOCK.acquire(blocking=False)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError("Concurrent fetch forbidden")
        assert discovery.refresh_checked_source("wikipedia", fetch=forbidden)["state"] == "unable_to_determine"
    finally:
        discovery.CHECK_LOCK.release()


def test_catalog_second_page_is_checked_before_picking_newest_candidate():
    # A complete first page cannot establish the latest publisher edition.
    first = feed(*(atom("2026-09") for _ in range(discovery.CATALOG_PAGE_SIZE)))
    second = feed(atom("2026-11"))
    calls = []

    def fetch(url, *, kind):
        calls.append((kind, url))
        if kind == "metalink":
            return metalink("2026-11")
        assert kind == "catalog"
        if "start=0&count=25" in url:
            return first
        if "start=25&count=25" in url:
            return second
        raise AssertionError("Unexpected page")

    result = discovery.check_kiwix_update(INSTALLED, fetch=fetch)
    assert result["state"] == "update_available"
    assert result["candidate_edition"] == "2026-11"
    assert [kind for kind, _ in calls] == ["catalog", "catalog", "metalink"]


def test_incomplete_bounded_catalog_does_not_claim_latest_candidate():
    full_page = feed(*(atom("2026-11") for _ in range(discovery.CATALOG_PAGE_SIZE)))
    calls = []

    def fetch(url, *, kind):
        calls.append((kind, url))
        assert kind == "catalog"
        return full_page

    result = discovery.check_kiwix_update(INSTALLED, fetch=fetch)
    assert result["state"] == "unable_to_determine"
    assert len(calls) == discovery.MAX_CATALOG_PAGES


def test_discovery_accepts_one_same_publisher_mirror_in_multi_mirror_metalink():
    calls = []
    doc = metalink("2026-11").replace(
        b"</file>",
        (b"<url>https://lb.download.kiwix.org/zim/wikipedia/"
         b"wikipedia_en_all_maxi_2026-11.zim</url></file>"),
    )
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(atom("2026-11")), doc, calls))
    assert result["state"] == "update_available"
    assert result["candidate_url"] == ROOT + FAMILY + "_2026-11.zim"
    assert [kind for kind, _ in calls] == ["catalog", "metalink"]


def test_discovery_uses_same_host_opds_url_when_metalink_lists_only_mirrors():
    calls = []
    doc = metalink("2026-11").replace(
        ("https://download.kiwix.org/zim/wikipedia/" + FAMILY + "_2026-11.zim").encode(),
        ("https://lb.download.kiwix.org/zim/wikipedia/" + FAMILY + "_2026-11.zim").encode(),
    )
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(atom("2026-11")), doc, calls))
    assert result["state"] == "update_available"
    assert result["candidate_url"] == ROOT + FAMILY + "_2026-11.zim"
    assert result["candidate_sha256"] == "b" * 64
    assert result["candidate_bytes"] == 123456

    alternate = atom("2026-11").replace("download.kiwix.org", "lb.download.kiwix.org")
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(alternate), doc, []))
    assert result["state"] == "unable_to_determine"


def test_ready_gov_dotted_family_can_discover_newer_verified_catalog_edition():
    family = "ready.gov_en_all"
    root = "https://download.kiwix.org/zim/zimit/"
    installed = dict(
        INSTALLED,
        family=family,
        edition="2026-10",
        source_url=root + family + "_2026-10.zim",
        language="eng",
        variant="all",
    )
    candidate_name = family + "_2026-11.zim"
    xml = (f'<feed xmlns="http://www.w3.org/2005/Atom">'
           f'<entry><id>urn:uuid:ready-gov-2026-11</id><title>Ready.gov</title>'
           f'<link rel="http://opds-spec.org/acquisition/open-access" '
           f'type="application/x-zim" href="{root}{candidate_name}.meta4"/>'
           f'</entry></feed>').encode()
    metalink_xml = (f'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
                    f'<file name="{candidate_name}"><size>123456</size>'
                    f'<hash type="sha-256">{"b" * 64}</hash>'
                    f'<url>{root}{candidate_name}</url></file></metalink>').encode()
    calls = []
    def fetch(url, *, kind):
        calls.append((kind, url))
        if kind == "catalog":
            assert "name=ready.gov_en_all" in url
            assert "lang=eng" in url
            return xml
        assert kind == "metalink"
        return metalink_xml
    result = discovery.check_kiwix_update(installed, fetch=fetch)
    assert result["state"] == "update_available"
    assert result["candidate_url"] == root + candidate_name
    assert [kind for kind, _ in calls] == ["catalog", "metalink"]


def test_no_newer_catalog_status_needs_complete_scan_and_matching_installed_entry():
    complete = discovery.check_kiwix_update(
        INSTALLED, fetch=fetcher(feed(atom("2026-08"), atom("2026-07")), b"", []))
    assert complete["state"] == "no_newer_edition_listed"
    assert complete["installed_edition"] == "2026-08"
    for raw in (feed(), feed(atom("2026-07")), b"invalid"):
        assert discovery.check_kiwix_update(
            INSTALLED, fetch=fetcher(raw, b"", []))["state"] == "unable_to_determine"


def test_journal_logs_catalog_counts_and_inconclusive_reason_without_remote_payload(monkeypatch):
    """Operator journal shows diagnostic stages, without echoing remote XML."""
    messages = []
    monkeypatch.setattr(discovery.LOGGER, "info",
                        lambda fmt, *args: messages.append(fmt % args))
    monkeypatch.setattr(discovery.LOGGER, "warning",
                        lambda fmt, *args: messages.append(fmt % args))
    monkeypatch.setattr(discovery.LOGGER, "log",
                        lambda level, fmt, *args: messages.append(fmt % args))

    # A real-looking, but deliberately unmatched catalog entry must fail closed.
    payload = feed(atom("2026-11", family="wikipedia_en_all_mini"))
    result = discovery.check_kiwix_update(
        INSTALLED, fetch=lambda url, *, kind: payload)
    assert result["state"] == "unable_to_determine"
    assert any("stage=catalog_scan page=0 entries=1 parsed=1" in msg
               for msg in messages)
    assert any("stage=catalog_match state=unable_to_determine" in msg
               for msg in messages)
    assert not any("https://" in msg or "<feed" in msg or "urn:uuid" in msg
                   for msg in messages)


def test_journal_logs_failure_stage_and_exception_class_only(monkeypatch):
    messages = []
    monkeypatch.setattr(discovery.LOGGER, "info", lambda *args: None)
    monkeypatch.setattr(discovery.LOGGER, "warning",
                        lambda fmt, *args: messages.append(fmt % args))
    monkeypatch.setattr(discovery.LOGGER, "log", lambda *args: None)

    secret = "https://publisher.invalid/secret?token=do-not-log"
    def failed_fetch(url, *, kind):
        raise ValueError(secret)
    result = discovery.check_kiwix_update(INSTALLED, fetch=failed_fetch)
    assert result["state"] == "unable_to_determine"
    assert any("stage=catalog metadata request exception_type=ValueError" in msg
               for msg in messages)
    assert all(secret not in msg for msg in messages)


def test_journal_reports_cache_hit_without_network(monkeypatch, tmp_path):
    from theaxiom import update_check_store, zim_jobs
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    job = installed_job_for_fixture()
    monkeypatch.setattr(zim_jobs, "status", lambda sid: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda j, config: j["artifact"])
    messages = []
    monkeypatch.setattr(discovery.LOGGER, "info",
                        lambda fmt, *args: messages.append(fmt % args))
    monkeypatch.setattr(discovery.LOGGER, "warning", lambda *args: None)
    monkeypatch.setattr(discovery.LOGGER, "log", lambda *args: None)
    fetch = fetcher(feed(atom("2026-08")), b"", [])
    assert discovery.refresh_checked_source("wikipedia", fetch=fetch)["state"] == "no_newer_edition_listed"
    assert discovery.refresh_checked_source("wikipedia", fetch=lambda *args, **kwargs: (
        _ for _ in ()).throw(AssertionError("cache should prevent network")))["state"] == "no_newer_edition_listed"
    assert any("stage=cache_hit state=no_newer_edition_listed" in line for line in messages)
    assert any("stage=metadata_check cache=miss" in line for line in messages)


def test_journal_reports_missing_job_before_discovery(monkeypatch):
    from theaxiom import zim_jobs
    monkeypatch.setattr(zim_jobs, "status", lambda sid: None)
    warnings = []
    monkeypatch.setattr(discovery.LOGGER, "info", lambda *args: None)
    monkeypatch.setattr(discovery.LOGGER, "warning",
                        lambda fmt, *args: warnings.append(fmt % args))
    assert discovery.refresh_checked_source("missing")["state"] == "unable_to_determine"
    assert any("stage=job_lookup" in msg for msg in warnings)


def test_verified_installed_zim_without_operator_supplied_sha_supports_update_discovery():
    """The independently measured digest is authoritative when the source was downloaded without a pre-pinned SHA."""
    job = installed_job_for_fixture()
    job["configuration"] = dict(job["configuration"], sha256="")
    installed = discovery.installed_identity_from_job(job, job["artifact"])
    assert installed == INSTALLED

    # An expected publisher pin, if supplied, must still agree with the
    # independently verified installed artifact.
    job["configuration"]["sha256"] = "b" * 64
    assert discovery.installed_identity_from_job(job, job["artifact"]) is None


def test_unpinned_sha_verified_fema_can_reach_opds_discovery(monkeypatch, tmp_path):
    from theaxiom import update_check_store, zim_jobs
    family = "ready.gov_en_all"
    root = "https://download.kiwix.org/zim/zimit/"
    job = installed_job_for_fixture()
    job["source_id"] = "fema-ready"
    job["configuration"] = {
        "format": "zim", "source_url": root + family + "_2026-10.zim",
        "expected_size_bytes": "", "sha256": "",
    }
    digest = "a" * 64
    job["artifact"] = {"filename": "fema-ready." + digest + ".zim",
                       "sha256": digest, "bytes": 123456}
    monkeypatch.setattr(zim_jobs, "status", lambda sid: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda j, config: j["artifact"])
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    calls = []
    def fetch(url, *, kind):
        calls.append((kind, url))
        assert kind == "catalog"
        assert "name=ready.gov_en_all" in url
        return feed(atom("2026-10", family=family))
    result = discovery.refresh_checked_source("fema-ready", fetch=fetch)
    assert result["state"] == "no_newer_edition_listed"
    assert len(calls) == 1


def test_deleted_update_stays_private_until_a_fresh_scheduled_check(monkeypatch, tmp_path):
    from theaxiom import zim_jobs, update_check_store

    sid = "wikipedia"
    monkeypatch.setattr(update_check_store, "root_path", lambda: tmp_path / "checks")
    job = installed_job_for_fixture()
    monkeypatch.setattr(zim_jobs, "status", lambda _: job)
    monkeypatch.setattr(zim_jobs, "installed", lambda live, config: live["artifact"])
    calls = []
    fetch = fetcher(feed(atom("2026-11")), metalink("2026-11"), calls)
    assert discovery.refresh_checked_source(sid, fetch=fetch)["state"] == "update_available"
    saved = update_check_store.record_path(sid).read_bytes()
    update_check_store.require_recheck(sid)

    assert discovery.cached_checked_source(sid) == {
        "state": "unable_to_determine", "evidence": "no current check evidence"}
    assert len(calls) == 2

    def offline(*args, **kwargs):
        raise OSError("offline")

    assert discovery.refresh_checked_source(sid, fetch=offline)["state"] == "unable_to_determine"
    assert update_check_store.record_path(sid).read_bytes() == saved
    assert "candidate_edition" not in discovery.cached_checked_source(sid)
    from theaxiom import kiwix_update_scheduler
    monkeypatch.setattr(discovery.kiwix_metadata_transport, "fetch_metadata", fetch)
    assert kiwix_update_scheduler.check_records([
        {"source_id": sid, "configuration": {"format": "zim", "update_periodic": "yes"}},
    ]) == [{"source_id": sid, "state": "update_available"}]
    assert len(calls) == 4
    assert discovery.cached_checked_source(sid)["candidate_edition"] == "2026-11"
