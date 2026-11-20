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
    assert "Not yet supported</span>" in page
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
    assert "Candidate source metadata is not trusted or fetched." not in page
    assert 'id="catalog-feedback" role="status" aria-live="polite"></p>' in page


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
                   'class="group-select" hidden', 'id="bulk-selection" aria-label="Selected source actions" hidden',
                   'id="bulk-actions" hidden', 'id="bulk-publish"',
                   'id="bulk-unpublish"', 'id="bulk-remove"'):
        assert marker in page
    assert "Confirm you have permission to make these documents available to library users" in page
    assert "currentRevision = result.revision" in page
    assert "outcome uncertain" in page


def test_source_editor_group_membership_controls():
    groups = [{"kind": "tags", "group_id": "winter", "name": "Winter", "source_ids": ["fixture"]}]
    page = render([profile()], catalog=dict(sources=[profile()], revision=1, groups=groups))
    for kind in ("categories", "tags", "collections"):
        assert f'data-editor-group="{kind}"' in page
    assert 'id="editor-new-tag"' in page
    assert 'value="winter" data-editor-member>Winter' in page
    assert "wasMember === option.checked" in page
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


def test_compact_source_layout_and_checkbox_editor():
    page = render([profile()])
    assert '<section class="source-filters" id="source-filters-panel" aria-label="Filter and sort sources" hidden>' in page
    assert page.index('id="source-filter-search"') < page.index('id="source-selection-bar"') < page.index('id="source-select-mode"') < page.index('id="source-filter-toggle"') < page.index('id="bulk-selection"') < page.index('id="group-panel"') < page.index('id="source-filters-panel"') < page.index('id="source-cards"')
    assert '<details id="source-editor-groups" hidden>' in page
    assert 'No categories available.' in page
    assert 'Add starter categories and tags' in page


def test_bulk_lifecycle_controls_separate_from_grouping_panel():
    page = render([profile()])
    selection = page.index('id="bulk-selection"')
    organize = page.index('id="group-panel"')
    assert page.index('id="source-selection-bar"') < selection < page.index('id="bulk-publish"') < organize
    assert '<strong>Selected source actions</strong>' not in page
    assert selection < page.index('id="bulk-unpublish"') < organize
    assert selection < page.index('id="bulk-remove"') < organize
    assert organize < page.index('id="group-assign"') < page.index('id="source-cards"')
    assert 'id="bulk-group-actions" hidden' in page


def test_minimal_source_list_toolbar():
    page = render([profile()])
    assert 'type="checkbox" id="group-select-all"' in page
    assert 'id="group-clear"' not in page
    assert 'id="source-filter-toggle"' in page
    assert 'aria-controls="source-filters-panel" aria-expanded="false"' in page
    assert 'selectAll.indeterminate' in page
    assert 'id="source-filter-search" type="search"' in page


def test_bulk_publication_requires_all_selected_sources_eligible():
    page = render([profile()])
    assert 'id="bulk-publish" disabled' in page
    assert 'id="bulk-unpublish" disabled' in page
    assert 'ids.length > 0 && ids.every(id =>' in page
    assert "card.dataset.filterPublication === 'unknown'" in page
    assert "toggle.dataset.acknowledgeRights === 'false'" not in page
    assert "if (rightsIds.has(source_id)) payload.acknowledge_rights = true" in page
    assert "else if (rightsIds.has(source_id))" in page
    assert "currentRevision += 1" in page
    assert "card.dataset.filterPublication === 'published' && toggle.dataset.enabled === 'true'" in page
    assert "action !== 'remove' && !bulkPublicationEligible(action, ids)" in page


def test_sources_page_omits_obsolete_intro():
    page = render([profile()])
    assert '<h2 id="sources">Content sources</h2>' not in page
    assert "Acquisition controls are disabled until safe download and recovery support is ready." not in page


def test_source_summary_is_compact_and_selection_is_separate():
    page = render([profile("field-guide", "Field guide")])
    card = page.split('<div class="source-row">', 1)[1].split('    {% else %}', 1)[0]
    summary = card.split('<summary class="source-header">', 1)[1].split('</summary>', 1)[0]
    assert 'source-symbol' in summary and 'summary-progress' in summary
    assert 'data-group-select' not in summary and '<button' not in summary and '<input' not in summary
    assert card.index('data-group-select') < card.index('<summary')
    assert card.index('<button class="button" type="button" data-catalog-edit=') > card.index('</summary>')
    assert "job?.active === true && !stale" in page
    assert "root.appendChild(card.closest('.source-row'))" in page


def test_document_card_actions_are_contextual():
    for format_name in ("pdf", "epub"):
        label = format_name.upper()
        manual = profile(format=format_name)
        page = render([manual])
        assert f">Upload {label}</button>" in page
        assert ">Download from URL</button>" not in page
        manual["installed_verified"] = True
        manual["artifact"] = dict(bytes=4096, sha256="a" * 64, filename="guide")
        page = render([manual])
        assert f">Replace {label}</button>" in page
        assert "Artifact size (bytes)</dt><dd>4096" in page
        assert "<dt>Author" not in page
        assert "Technical details</summary>" in page
        url = profile(format=format_name, source_url="https://example.org/guide." + format_name)
        page = render([url])
        assert ">Download from URL</button>" in page
        assert f">Upload {label}</button>" not in page
        url["installed_verified"] = True
        assert ">Re-download</button>" in render([url])
        invalid = profile(format=format_name, source_url="http://example.org/guide." + format_name)
        assert f">Upload {label}</button>" not in render([invalid])
        manual["publication"] = dict(enabled=True, published=True)
        parser = Parser()
        parser.feed(render([manual]))
        assert all("hidden" in attrs and "disabled" in attrs
                   for tag, attrs in parser.tags if tag == "button" and "data-pdf-upload" in attrs)
    for format_name in ("other", "zim"):
        parser = Parser()
        parser.feed(render([profile(format=format_name)]))
        assert not any("data-pdf-upload" in attrs or "data-pdf-download" in attrs for _, attrs in parser.tags)


def test_compact_card_and_acquisition_feedback():
    page = render([profile()])
    card = page.split('<div class="source-expanded">', 1)[1].split('</details></div>', 1)[0]
    assert card.count('class="source-actions"') == 1
    assert "Download from the configured HTTPS URL or upload a PDF from your computer." not in card
    assert "Candidate " not in card
    assert 'id="source-form-busy"' in page
    assert 'data-pdf-busy hidden' in card
    assert "if (formPosting) return;" in page
    assert "Acquisition outcome uncertain" in page
    assert "cancelSave.disabled = false;" in page
