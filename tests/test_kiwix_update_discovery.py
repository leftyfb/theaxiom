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
