from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment
from theaxiom.admin_api import WIKIPEDIA_SOURCE

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def render(**overrides):
    values = dict(offline_library_content_manifest=[], offline_library_kiwix_zim_enabled=True,
                  offline_library_kiwix_zim=WIKIPEDIA_SOURCE)
    values.update(overrides)
    return Environment(autoescape=True).from_string(TEMPLATE.read_text()).render(**values)


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_source_card_and_disabled_actions():
    parser = Parser()
    parser.feed(render())
    assert sum(tag == "details" and attrs.get("data-source-id") == "wikipedia-en"
               for tag, attrs in parser.tags) == 1
    actions = [attrs for tag, attrs in parser.tags if tag == "button" and "data-action" in attrs]
    assert {a["data-action"] for a in actions} == {"download-zim", "redownload-zim", "update-zim"}
    assert all("disabled" in a for a in actions)
    assert all("open" not in attrs for tag, attrs in parser.tags if tag == "details")
    verify = next(attrs for tag, attrs in parser.tags if attrs.get("data-field") == "verification")
    assert "value" not in verify


def test_manifest_and_empty_cards():
    page = render(offline_library_kiwix_zim_enabled=False,
                  offline_library_content_manifest=[{"title": "Fixture", "kind": "pdf"}])
    assert "Fixture" in page and "Status unavailable" in page
    assert "No managed sources configured." in render(offline_library_kiwix_zim_enabled=False)


def test_page_specific_scripts_and_navigation():
    sources = render(admin_page="sources", password_change_enabled=True)
    settings = render(admin_page="settings", password_change_enabled=True)
    assert 'href="/admin/sources" aria-current="page"' in sources
    assert 'href="/admin/settings" aria-current="page"' in settings
    for marker in ("acquisition-monitor-script", "publication-script", "source-cards"):
        assert marker in sources and marker not in settings
    for marker in ("storage-settings-script", "admin-password-script", "storage-form"):
        assert marker in settings and marker not in sources


def test_planned_card_has_no_action_buttons():
    page = render(offline_library_kiwix_zim_enabled=False,
                  offline_library_content_manifest=[{"title": "Fixture", "kind": "planned", "description": "Emergency reference"}])
    parser = Parser()
    parser.feed(page)
    assert "Not downloaded" in page
    assert "Emergency reference" in page
    assert "Download not supported yet." in page
    assert "Coming soon" not in page
    assert "Planned collection" not in page
    assert not any(tag == "button" and attrs.get("data-acquisition-action") for tag, attrs in parser.tags)
    assert all("open" not in attrs for tag, attrs in parser.tags if tag == "details")


def test_catalog_management_form_exposes_metadata_and_confirmation_controls():
    page = render(
        admin_page="sources",
        catalog_custom_sources=[{
            "source_id": "field-guide", "title": "Field guide", "description": "Reference",
            "source_url": "https://example.org/guide?topic=a&part=b", "publisher": "Example",
            "edition": "2026", "language": "en", "expected_size_bytes": "4096",
            "sha256": "a" * 64, "provenance": "Verified metadata source", "license": "CC BY 4.0", "builtin": False,
        }],
    )
    assert '<dialog id="source-dialog"' in page
    assert 'id="source-form"' in page
    for field in ("source_id", "title", "description", "source_url", "publisher", "edition", "language", "expected_size_bytes", "sha256", "provenance", "license"):
        assert f'name="{field}"' in page
    assert 'data-catalog-edit="field-guide"' in page
    assert 'data-catalog-source-url="https://example.org/guide?topic=a&amp;part=b"' in page
    assert 'data-catalog-expected-size-bytes="4096"' in page
    assert 'data-catalog-sha256="' + ("a" * 64) + '"' in page
    assert 'data-catalog-remove="field-guide"' in page
    assert "Installed artifacts will not be deleted." in page
    assert "window.prompt" not in page
    assert "Candidate source metadata is not trusted or fetched." in page


def test_admin_branding_and_favicon():
    page = render()
    assert 'href="/favicon.svg"' in page
    assert "Administration · The Axiom" in page
    assert "← The Axiom" in page
