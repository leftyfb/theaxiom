"""Catalog, document pages, static assets, and checksums stay aligned."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[1]

STARTER_DOCUMENTS = {
    "/content/emergency-disinfection-of-drinking-water-sept2017.pdf": (ROOT / "fixtures/kiwix/content/emergency-disinfection-of-drinking-water-sept2017.pdf", "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5"),
    "/content/handwashing-cdc-11964.pdf": (ROOT / "fixtures/kiwix/content/handwashing-cdc-11964.pdf", "b7095c42196a6b63e91210c5fe2c7c9d3792d5f95718e3bf137270d18f533bc8"),
    "/content/ready-power-outage-info-sheet.pdf": (ROOT / "fixtures/kiwix/content/ready-power-outage-info-sheet.pdf", "d98a6ca01ba91c8b5a6d4772ce46dec7ecc8481eab5778373b5d1b54e49f4697"),
}


def test_starter_pdf_routes_exist_and_match_pinned_checksums() -> None:
    for route, (document, checksum) in STARTER_DOCUMENTS.items():
        assert route in (ROOT / "config/catalog.yaml").read_text(encoding="utf-8")
        assert document.is_file() and document.read_bytes().startswith(b"%PDF-")
        assert hashlib.sha256(document.read_bytes()).hexdigest() == checksum


def test_document_and_homepage_templates_preserve_routes_and_provenance() -> None:
    homepage = (ROOT / "templates/kiwix-homepage.html.j2").read_text(encoding="utf-8")
    page = (ROOT / "templates/document-page.html.j2").read_text(encoding="utf-8")
    for marker in ('href="{{ document.route }}"', 'href="{{ document.asset_route }}"', 'href="{{ document.download_route }}"'):
        assert marker in homepage or marker in page
    for marker in ("document.publisher", "document.source_url", "document.sha256", "Return to catalog"):
        assert marker in page
