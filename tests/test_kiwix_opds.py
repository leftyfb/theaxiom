"""Offline, immutable-fixture tests for Kiwix Atom OPDS parsing."""
import pytest

from theaxiom import kiwix_opds

HEADER = b'<feed xmlns="http://www.w3.org/2005/Atom">'
FOOTER = b'</feed>'
LINK = (b'<link rel="http://opds-spec.org/acquisition/open-access" '
        b'type="application/x-zim" '
        b'href="https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-11.zim.meta4"/>')
ENTRY = b'<entry><id>uuid-1</id><title>Wikipedia English</title>' + LINK + b'</entry>'


def test_parses_metalink_without_fabricating_direct_zim_or_digest():
    items = kiwix_opds.parse_acquisition_feed(HEADER + ENTRY + FOOTER)
    assert len(items) == 1
    assert items[0]["acquisition_kind"] == "metalink"
    assert items[0]["acquisition_url"].endswith(".zim.meta4")
    assert items[0]["verification"] == "unverified"
    assert "sha256" not in items[0]
    assert "edition" not in items[0]


def test_direct_zim_is_still_unverified():
    item = ENTRY.replace(b".zim.meta4", b".zim")
    result = kiwix_opds.parse_acquisition_feed(HEADER + item + FOOTER)
    assert result[0]["acquisition_kind"] == "direct"
    assert result[0]["verification"] == "unverified"


@pytest.mark.parametrize("url", [
    "http://download.kiwix.org/zim/test.zim",
    "https://evil.invalid/zim/test.zim",
    "https://download.kiwix.org.evil.invalid/zim/test.zim",
    "https://user@download.kiwix.org/zim/test.zim",
    "https://download.kiwix.org:444/zim/test.zim",
    "https://download.kiwix.org/zim/test.zim?token=1",
    "https://download.kiwix.org/zim/test.zim#fragment",
    "https://download.kiwix.org/zim/test.zip",
])
def test_unsafe_or_irrelevant_link_is_ignored(url):
    data = ENTRY.replace(
        b"https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-11.zim.meta4",
        url.encode(),
    )
    assert kiwix_opds.parse_acquisition_feed(HEADER + data + FOOTER) == []


@pytest.mark.parametrize("xml", [
    b'<!DOCTYPE feed [<!ENTITY p "expanded">]>' + HEADER + ENTRY + FOOTER,
    b'<!doctype feed [<!entity p "expanded">]>' + HEADER + ENTRY + FOOTER,
    b'<feed></feed>',
    b'<html><body/></html>',
    b'not xml',
    b'',
    b'x' * (kiwix_opds.MAX_BYTES + 1),
])
def test_malformed_or_untrusted_xml_fails_closed(xml):
    with pytest.raises(ValueError):
        kiwix_opds.parse_acquisition_feed(xml)


def test_excessive_entries_fail_closed():
    xml = HEADER + ENTRY * (kiwix_opds.MAX_ENTRIES + 1) + FOOTER
    with pytest.raises(ValueError, match="Too many"):
        kiwix_opds.parse_acquisition_feed(xml)


def test_wrong_acquisition_type_and_missing_metadata_do_not_create_candidates():
    xml = HEADER + ENTRY.replace(b'application/x-zim', b'text/html') + FOOTER
    assert kiwix_opds.parse_acquisition_feed(xml) == []
    xml = HEADER + b'<entry>' + LINK + b'</entry>' + FOOTER
    assert kiwix_opds.parse_acquisition_feed(xml) == []


def test_parse_has_no_network_side_effects(monkeypatch):
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("Parsing must never access network")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert len(kiwix_opds.parse_acquisition_feed(HEADER + ENTRY + FOOTER)) == 1


METALINK_HEADER = b'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
METALINK_FOOTER = b'</metalink>'
META_FILENAME = "wikipedia_en_all_maxi_2026-11.zim"
META_FILE = (
    b'<file name="wikipedia_en_all_maxi_2026-11.zim">'
    b'<size>123456</size><hash type="sha-256">' + b'a' * 64 +
    b'</hash><url>https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-11.zim</url>'
    b'</file>'
)


def test_metalink_explicit_digest_and_url_are_metadata_only():
    result = kiwix_opds.parse_metalink(
        METALINK_HEADER + META_FILE + METALINK_FOOTER,
        expected_filename=META_FILENAME,
    )
    assert result == {
        "filename": META_FILENAME,
        "source_url": "https://download.kiwix.org/zim/wikipedia/" + META_FILENAME,
        "bytes": 123456, "sha256": "a" * 64,
        "verification": "metadata_only",
    }


@pytest.mark.parametrize("alteration", [
    lambda d: d.replace(b'<size>123456</size>', b'<size>0</size>'),
    lambda d: d.replace(b'<size>123456</size>', b'<size>nope</size>'),
    lambda d: d.replace(b'a' * 64, b'g' * 64),
    lambda d: d.replace(b'https://download.kiwix.org', b'https://evil.invalid'),
    lambda d: d.replace(b'.zim</url>', b'.zim.meta4</url>'),
    lambda d: d.replace(b'.zim</url>', b'.zip</url>'),
    lambda d: d.replace(b'</file>', b'<url>https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-11.zim</url></file>'),
    lambda d: d.replace(b'</file>', b'<hash type="sha-256">' + b'b' * 64 + b'</hash></file>'),
])
def test_metalink_must_have_unambiguous_safe_metadata(alteration):
    with pytest.raises(ValueError):
        kiwix_opds.parse_metalink(
            METALINK_HEADER + alteration(META_FILE) + METALINK_FOOTER,
            expected_filename=META_FILENAME,
        )


def test_metalink_refuses_mismatch_dtd_and_oversized():
    xml = METALINK_HEADER + META_FILE + METALINK_FOOTER
    for filename in ("other.zim", "../bad.zim", "bad.zip", "wikipedia_2026-11.zim.meta4"):
        with pytest.raises(ValueError):
            kiwix_opds.parse_metalink(xml, expected_filename=filename)
    with pytest.raises(ValueError):
        kiwix_opds.parse_metalink(b'<!DOCTYPE metalink>' + xml, expected_filename=META_FILENAME)
    with pytest.raises(ValueError):
        kiwix_opds.parse_metalink(b'x' * (kiwix_opds.MAX_METALINK_BYTES + 1),
                                 expected_filename=META_FILENAME)
