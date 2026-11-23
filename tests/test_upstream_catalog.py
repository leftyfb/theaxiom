from unittest.mock import Mock

import pytest

from theaxiom import upstream_catalog as catalog


FEED = b'''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Reference</title><id>ref</id><rights>Publisher terms</rights>
  <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
        href="https://download.kiwix.org/zim/reference/reference_eng_maxi_2026-10.zim" length="2048"/>
  <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
        href="https://download.kiwix.org/zim/reference/reference_eng_maxi_2026-11.zim.meta4" length="500"/>
  <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
        href="https://download.kiwix.org/zim/reference/other_eng_2026-11.zim" length="3000"/>
  </entry></feed>'''


def test_exact_query_falls_back_to_last_publication_listings(tmp_path, monkeypatch):
    path = tmp_path / "metadata"
    monkeypatch.setattr(catalog, "_path", lambda: path)
    clock = Mock(return_value=1000)
    monkeypatch.setattr(catalog.time, "time", clock)
    monkeypatch.setattr(catalog, "_next_refresh", 0)
    fetch = Mock(return_value=FEED)
    monkeypatch.setattr(catalog.kiwix_metadata_transport, "fetch_metadata", fetch)

    first = catalog.refresh("reference_eng", "eng")
    assert not first["cached"]
    assert first["entries"] == [
        {"title": "Reference", "edition": "2026-11", "variant": "maxi",
         "advertised_size": None, "rights": "Publisher terms", "publisher_checksum_listed": True},
        {"title": "Reference", "edition": "2026-10", "variant": "maxi",
         "advertised_size": "2048", "rights": "Publisher terms", "publisher_checksum_listed": False},
    ]
    fetch.assert_called_once_with(
        "https://opds.library.kiwix.org/catalog/v2/entries?name=reference_eng&lang=eng&start=0&count=25",
        kind="catalog",
    )
    assert catalog.refresh("reference_eng", "eng")["cached"]
    fetch.assert_called_once()  # Within the refresh cooldown.

    monkeypatch.setattr(catalog, "_next_refresh", 0)
    fetch.side_effect = OSError("offline")
    clock.return_value += catalog.STALE_AFTER + 1
    offline = catalog.refresh("reference_eng", "eng")
    assert offline["cached"] and offline["stale"] and offline["checked_at"] == 1000
    assert offline["entries"] == first["entries"]
    assert catalog.refresh("reference_eng", "fra")["unavailable"]
    assert catalog.refresh("another", "eng")["unavailable"]


def test_invalid_response_never_becomes_cached(tmp_path, monkeypatch):
    path = tmp_path / "metadata"
    monkeypatch.setattr(catalog, "_path", lambda: path)
    monkeypatch.setattr(catalog, "_next_refresh", 0)
    fetch = Mock(return_value=b"x" * (catalog.kiwix_opds.MAX_BYTES + 1))
    monkeypatch.setattr(catalog.kiwix_metadata_transport, "fetch_metadata", fetch)
    with pytest.raises(ValueError):
        catalog.refresh("https://unknown.example", "eng")
    fetch.assert_not_called()
    assert catalog.refresh("reference_eng", "eng")["unavailable"]
    assert not path.exists()
    monkeypatch.setattr(catalog, "_next_refresh", 0)
    fetch.return_value = FEED.decode().encode("utf-16")
    assert catalog.refresh("reference_eng", "eng")["unavailable"]
    assert not path.exists()


def test_inspect_edition_reads_only_cached_exact_listing_and_publisher_metalink(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_path", lambda: tmp_path / "metadata")
    monkeypatch.setattr(catalog, "_next_refresh", 0)
    monkeypatch.setattr(catalog, "_next_inspect", 0)
    name = "reference_eng_maxi_2026-11.zim"
    url = "https://download.kiwix.org/zim/reference/" + name
    digest = "a" * 64
    metalink = (f'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="{name}">'
                f'<size>4096</size><hash type="sha-256">{digest}</hash><url>{url}</url>'
                '</file></metalink>').encode()
    fetch = Mock(side_effect=[FEED, metalink])
    monkeypatch.setattr(catalog.kiwix_metadata_transport, "fetch_metadata", fetch)
    catalog.refresh("reference_eng", "eng")
    with pytest.raises(ValueError, match="Refresh"):
        catalog.inspect_edition("reference_eng", "fra", "2026-11", "maxi")
    with pytest.raises(ValueError, match="No unique"):
        catalog.inspect_edition("reference_eng", "eng", "2026-10", "maxi")
    details = catalog.inspect_edition("reference_eng", "eng", "2026-11", "maxi")
    assert details == {
        "filename": name, "bytes": "4096", "sha256": digest,
        "draft": {"title": "Reference", "format": "zim", "source_url": url,
                  "edition": "2026-11", "language": "eng", "expected_size_bytes": "4096",
                  "sha256": digest, "license": "Publisher terms",
                  "provenance": "Kiwix OPDS and Metalink publisher metadata"},
    }
    assert fetch.call_count == 2
    assert fetch.call_args.args == (url + ".meta4",)
    assert fetch.call_args.kwargs == {"kind": "metalink-inspection"}

    monkeypatch.setattr(catalog, "_next_inspect", 0)
    fetch.side_effect = None
    fetch.return_value = metalink.replace(b"/zim/reference/", b"/zim/another-family/")
    assert catalog.inspect_edition("reference_eng", "eng", "2026-11", "maxi") == {
        "filename": name, "bytes": "4096", "sha256": digest,
    }
    assert fetch.call_count == 3
    assert fetch.call_args.args == (url + ".meta4",)


def test_large_inspection_only_metadata_preserves_acquisition_limits():
    name = "reference_eng_2026-11.zim"
    url = "https://download.kiwix.org/zim/reference/" + name + ".meta4"
    _, _, inspect_cap = catalog.kiwix_metadata_transport._validated_endpoint(url, "metalink-inspection")
    _, _, acquire_cap = catalog.kiwix_metadata_transport._validated_endpoint(url, "metalink")
    assert acquire_cap == catalog.kiwix_opds.MAX_METALINK_BYTES < inspect_cap
    digest = "b" * 64
    raw = (f'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="{name}">'
           f'<size>4096</size><hash type="sha-256">{digest}</hash>'
           '<!--' + 'x' * acquire_cap + '--></file></metalink>').encode()
    assert catalog.kiwix_opds.inspect_metalink(raw, expected_filename=name) == {
        "filename": name, "bytes": "4096", "sha256": digest,
    }
    with pytest.raises(ValueError, match="size"):
        catalog.kiwix_opds.parse_metalink(raw, expected_filename=name)
    with pytest.raises(ValueError, match="size"):
        catalog.kiwix_opds.inspect_metalink(raw + b" " * inspect_cap, expected_filename=name)
