"""Contract checks for the single explicitly opt-in Kiwix ZIM source."""
from theaxiom.admin_api import WIKIPEDIA_SOURCE, TRUSTED_SOURCES, resolve_source


def test_approved_zim_source_metadata_is_canonical() -> None:
    source = WIKIPEDIA_SOURCE
    assert source["source_id"] == "wikipedia-en"
    assert source["filename"] == "wikipedia_en_all_maxi_2026-08.zim"
    assert source["source_url"] == "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"
    assert source["sha256"] == "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6"
    assert source["bytes"] == 127418087648
    assert source["license"].startswith("CC BY-SA 4.0 and GFDL")
    assert source["rights_review_status"] == "reviewed-for-whole-zim-redistribution"


def test_zim_source_is_explicitly_trusted_and_resolvable() -> None:
    assert TRUSTED_SOURCES == {"wikipedia-en": WIKIPEDIA_SOURCE}
    assert resolve_source("wikipedia-en") is WIKIPEDIA_SOURCE
    assert resolve_source("wikipedia") is None


def test_kiwix_library_template_keeps_zim_opt_in() -> None:
    from pathlib import Path
    template = (Path(__file__).parents[1] / "templates/kiwix-library.xml.j2").read_text(encoding="utf-8")
    assert "offline_library_kiwix_zim_enabled" in template
    assert 'path="{{ offline_library_kiwix_zim_destination }}"' in template
    assert "tiny.txt" not in template
