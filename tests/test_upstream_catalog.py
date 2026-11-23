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
         "advertised_size": None, "rights": "Publisher terms"},
        {"title": "Reference", "edition": "2026-10", "variant": "maxi",
         "advertised_size": "2048", "rights": "Publisher terms"},
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
