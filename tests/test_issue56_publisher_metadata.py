"""One-time publisher metadata check for #56; no archive acquisition.

Remove this test after the verified upstream size and digest have been pinned.
"""
import os
from urllib.parse import urlsplit

import pytest

from theaxiom import kiwix_metadata_transport as transport, kiwix_opds

@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true", reason="one-time CI publisher metadata check")
def test_issue_56_live_publisher_metadata(capsys):
    expected = "mdwiki_en_all_maxi_2025-11.zim"
    matches = []
    for family in ("mdwiki_en_all", "wikipedia_en_medicine"):
        query = transport.catalog_page_url(family=family, language="eng")
        raw = transport.fetch_metadata(query, kind="catalog")
        entries = kiwix_opds.parse_acquisition_feed(raw, include_catalog_metadata=True)
        with capsys.disabled():
            print("\nISSUE56 CATALOG", family, [(e["title"], e["acquisition_url"], e.get("rights"), e.get("advertised_size")) for e in entries], flush=True)
        matches.extend(e for e in entries if e["acquisition_kind"] == "metalink"
                       and urlsplit(e["acquisition_url"]).path.endswith("/" + expected + ".meta4"))
    assert len(matches) == 1, "Expected one publisher-advertised MDWiki edition"
    link = matches[0]["acquisition_url"]
    raw = transport.fetch_metadata(link, kind="metalink-inspection")
    meta = kiwix_opds.inspect_metalink(raw, expected_filename=expected)
    with capsys.disabled():
        print("\nISSUE56 METALINK", meta, "LINK", link, flush=True)
