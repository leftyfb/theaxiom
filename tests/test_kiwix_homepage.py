"""Homepage contract for offline catalog navigation."""
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates/kiwix-homepage.html.j2"


def test_homepage_exposes_local_catalog_and_document_routes() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        '<a href="/catalog.json">Machine-readable catalog</a>',
        'href="{{ document.route }}"',
        'href="{{ document.asset_route }}"',
        'href="{{ document.download_route }}"',
        'href="/kiwix/"',
    ):
        assert marker in template
    assert template.count("/catalog.json") == 1
    assert "http://" not in template and "https://" not in template


def test_homepage_keeps_kiwix_section_opt_in() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "offline_library_kiwix_server_enabled and offline_library_kiwix_zim_enabled" in template
