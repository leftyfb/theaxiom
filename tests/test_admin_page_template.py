from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment
from theaxiom.admin_api import WIKIPEDIA_SOURCE
from theaxiom.catalog_store import EDITABLE

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def profile(source_id="fixture", title="Fixture", description="", artifact=None, **configuration):
    return dict(source_id=source_id, profile=dict(title=title, description=description),
                configuration={"format": "pdf", **configuration}, artifact=artifact,
                publication=None, mutation_blocked=False)


def render(sources=(), **overrides):
    values = dict(catalog=dict(sources=sources, revision=0), editable_fields=sorted(EDITABLE))
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
    parser.feed(render([profile("wikipedia-en", artifact=WIKIPEDIA_SOURCE, format="zim")]))
    assert sum(tag == "details" and attrs.get("data-source-id") == "wikipedia-en"
               for tag, attrs in parser.tags) == 1
    actions = [attrs for tag, attrs in parser.tags if tag == "button" and "data-action" in attrs]
    assert {a["data-action"] for a in actions} == {"download", "redownload", "update"}
    assert all("disabled" in a for a in actions)
    assert all("open" not in attrs for tag, attrs in parser.tags if tag == "details")
    verify = next(attrs for tag, attrs in parser.tags if attrs.get("data-field") == "verification")
    assert "value" not in verify


def test_configured_and_empty_cards():
    page = render([profile()])
    assert "Fixture" in page and "Candidate only" in page
    assert "No managed sources configured." in render()


def test_page_specific_scripts_and_navigation():
    sources = render(admin_page="sources", password_change_enabled=True)
    settings = render(admin_page="settings", password_change_enabled=True)
    assert 'href="/admin/sources" aria-current="page"' in sources
    assert 'href="/admin/settings" aria-current="page"' in settings
    for marker in ("acquisition-monitor-script", "source-catalog-script", "source-cards"):
        assert marker in sources and marker not in settings
    for marker in ("storage-settings-script", "admin-password-script", "storage-form"):
        assert marker in settings and marker not in sources


def test_candidate_card_has_no_acquisition_buttons():
    page = render([profile(description="Emergency reference")])
    parser = Parser()
    parser.feed(page)
    assert "Candidate only" in page
    assert "Emergency reference" in page
    assert "Download not supported for this configuration." in page
    assert "Coming soon" not in page
    assert "Planned collection" not in page
    assert not any(tag == "button" and attrs.get("data-action") for tag, attrs in parser.tags)
    assert all("open" not in attrs for tag, attrs in parser.tags if tag == "details")


def test_catalog_management_form_exposes_metadata_and_confirmation_controls():
    page = render(
        [profile("field-guide", "Field guide", "Reference",
                 source_url="https://example.org/guide?topic=a&part=b", expected_size_bytes="4096",
                 sha256="a" * 64, provenance="Publisher catalog", license="CC BY 4.0")],
    )
    assert '<dialog id="source-dialog"' in page
    assert 'id="source-form"' in page
    for field in EDITABLE | {"source_id"}:
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
