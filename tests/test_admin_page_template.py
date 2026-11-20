from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment
from theaxiom.admin_api import ACTION_LABELS, WIKIPEDIA_SOURCE, select_lifecycle
from theaxiom.catalog_store import EDITABLE

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def profile(source_id="fixture", title="Fixture", description="", **configuration):
    configuration = {"format": "pdf", **configuration}
    return dict(source_id=source_id, profile=dict(title=title, description=description),
                configuration=configuration, **select_lifecycle(source_id, configuration),
                publication=None, installed_verified=False, mutation_blocked=False)


def render(sources=(), **overrides):
    values = dict(catalog=dict(sources=sources, revision=0), editable_fields=sorted(EDITABLE),
                  action_labels=ACTION_LABELS)
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
    pin = WIKIPEDIA_SOURCE
    parser.feed(render([profile("wikipedia-en", format="zim", source_url=pin["source_url"],
                                expected_size_bytes=str(pin["bytes"]), sha256=pin["sha256"])]))
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
    assert "Fixture" in page and "Not downloaded" in page
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
    page = render([profile(description="Emergency reference", format="other")])
    parser = Parser()
    parser.feed(page)
    assert "Not downloaded</span>" in page
    assert "Emergency reference" in page
    assert "No lifecycle adapter supports this format." in page
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
    assert "It will be unpublished automatically. Installed files will not be deleted." in page
    assert "window.prompt" not in page
    assert "Candidate source metadata is not trusted or fetched." in page


def test_admin_branding_and_favicon():
    page = render()
    assert 'href="/favicon.svg"' in page
    assert "Administration · The Axiom" in page
    assert "← The Axiom" in page


def test_source_editor_simple_defaults_and_advanced_details():
    page = render()
    dialog = page.split('<dialog id="source-dialog"', 1)[1].split('</dialog>', 1)[0]
    basic, advanced = dialog.split('<details id="source-advanced">', 1)
    for name in ("title", "source_url", "format", "description"):
        assert f'name="{name}"' in basic
    for name in ("publisher", "edition", "language", "expected_size_bytes", "sha256", "provenance", "license"):
        assert f'name="{name}"' not in basic
        assert f'name="{name}"' in advanced
    assert 'name="source_id" type="hidden"' in basic
    assert '<option value="zim">' in basic and '<option value="pdf">' in basic
    assert "generateId()" in page and "data-legacy-format" in page
    assert 'href="#source-help"' in basic


def test_offline_source_guide_is_discoverable_and_honest():
    page = render()
    assert 'id="source-guide-open"' in page
    assert '<dialog id="source-guide"' in page
    assert 'https://download.kiwix.org/zim/' in page
    assert 'https://www.ready.gov/' in page
    assert 'explicitly configured NFS storage is supported' in page
    assert 'arbitrary remote downloads are not yet enabled' in page
    assert 'Removing a published profile automatically unpublishes it and does not delete installed files' in page
    assert 'id="source-guide"' not in render(admin_page="settings")


def test_group_management_controls_are_available():
    page = render([profile("field-guide", "Field guide", "Reference")])
    for marker in ('id="group-panel"', 'id="group-defaults"', 'id="group-create-assign"',
                   'id="group-assign"', 'id="group-unassign"', 'data-group-select value="field-guide"'):
        assert marker in page


def test_source_filter_toolbar():
    page = render([profile("field-guide", "Field guide", "Reference")])
    for marker in ('id="source-filter-search"', 'id="source-filter-publication"',
                   'id="source-filter-download"', 'id="source-filter-format"',
                   'id="source-filter-categories"', 'id="source-filter-collections"',
                   'id="source-filter-tags"', 'id="source-filter-sort"',
                   'value="published"', 'value="unpublished"', 'value="downloaded"',
                   'value="not-downloaded"', 'id="source-filter-empty"'):
        assert marker in page


def test_selection_mode_and_bulk_lifecycle_controls():
    page = render([profile("field-guide", "Field guide")])
    for marker in ('id="source-select-mode" aria-pressed="false"',
                   'class="group-select" hidden', 'id="bulk-selection" hidden',
                   'id="bulk-actions" hidden', 'id="bulk-publish"',
                   'id="bulk-unpublish"', 'id="bulk-remove"'):
        assert marker in page
    assert "Only eligible sources with existing rights approval" in page
    assert "currentRevision = result.revision" in page
    assert "outcome uncertain" in page


def test_source_editor_group_membership_controls():
    groups = [{"kind": "tags", "group_id": "winter", "name": "Winter", "source_ids": ["fixture"]}]
    page = render([profile()], catalog=dict(sources=[profile()], revision=1, groups=groups))
    for kind in ("categories", "tags", "collections"):
        assert f'data-editor-group="{kind}" multiple' in page
    assert 'id="editor-new-tag"' in page
    assert '<option value="winter">Winter</option>' in page
    assert "wasMember === option.selected" in page
    assert "grouping changes may be incomplete" in page


def test_standalone_groupings_page():
    groups = [{"kind":"tags","group_id":"winter","name":"Winter","source_ids":["fixture"]}]
    page = render(admin_page="groupings", catalog=dict(sources=[],revision=1,groups=groups))
    assert 'href="/admin/groupings" aria-current="page"' in page
    for marker in ('id="manage-kind"', 'id="manage-search"', 'id="manage-create"', 'id="manage-list"'):
        assert marker in page
    assert 'Winter' in page and 'source assignments?' in page
    assert 'source-catalog-script' not in page


def test_bulk_group_multi_select_and_replace_controls():
    page = render([profile()])
    assert 'id="group-existing" multiple' in page
    assert 'id="group-replace"' in page
    assert 'id="bulk-group-fields" hidden' in page
    assert "Other grouping types are preserved." in page
