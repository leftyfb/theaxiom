from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from theaxiom.admin_api import ACTION_LABELS, select_lifecycle
from theaxiom.source_catalog import WIKIPEDIA_SOURCE
from theaxiom.catalog_store import EDITABLE
from theaxiom import zim_catalog

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
    return Environment(loader=FileSystemLoader(TEMPLATE.parent), autoescape=True).get_template(TEMPLATE.name).render(**values)


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_pinned_source_card_omits_inert_legacy_actions():
    parser = Parser()
    pin = WIKIPEDIA_SOURCE
    parser.feed(render([profile("wikipedia-en", format="zim", source_url=pin["source_url"],
                                expected_size_bytes=str(pin["bytes"]), sha256=pin["sha256"])]))
    assert sum(tag == "details" and attrs.get("data-source-id") == "wikipedia-en"
               for tag, attrs in parser.tags) == 1
    actions = [attrs for tag, attrs in parser.tags if tag == "button" and "data-action" in attrs]
    assert actions == []
    assert any("data-zim-update-check" in attrs for tag, attrs in parser.tags if tag == "button")
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
    groupings = render(admin_page="groupings", password_change_enabled=True)
    assert "manage-kind" in groupings
    assert "storage-settings-script" not in groupings
    assert "admin-password-script" not in groupings
    assert "source-catalog-script" not in groupings
    updates = render(admin_page="updates", password_change_enabled=True)
    assert 'href="/admin/updates" aria-current="page"' in updates
    assert 'id="review-updates-link"' not in sources
    assert sources.count('data-update-nav-count aria-hidden="true" hidden') == 3
    for page in (sources, settings, groupings):
        assert 'href="/admin/updates"' in page
        assert 'id="update-review-script"' not in page
    for marker in ("update-review-script", "download-updates", "publish-updates",
                   "update-filter-state", "updates-pause"):
        assert marker in updates
    for marker in ("source-cards", "acquisition-monitor-script", "storage-form", "source-catalog-script"):
        assert marker not in updates


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
    assert 'name="update_periodic"' not in page
    import re
    policy = page.split('id="catalog-update-policy"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="([^"]*)"', policy) == ["notify", "automatic", "scheduled", "disabled"]
    for field in (EDITABLE - {"icon_png", "update_periodic"}) | {"source_id"}:
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


def test_candidate_storage_estimate_includes_verified_installed_bytes_only():
    source = profile("candidate-fixture", format="zim", source_url="https://example.org/reference.zim")
    source.update(update_check={"state": "update_available", "candidate_bytes": 1048576},
                  artifact={"bytes": 2097152})
    output = render([source])
    assert "Estimated transfer and staging: 1.0 MB" in output
    assert "Installed and candidate archives together: unknown" in " ".join(output.split())
    source["installed_verified"] = True
    output = render([source])
    assert "Installed and candidate archives together: 3.1 MB" in " ".join(output.split())


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
    assert 'Set a supported HTTPS ZIM download URL' in page
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
                   'id="source-filter-tags"',
                   'value="published"', 'value="unpublished"', 'value="downloaded"',
                   'value="not-downloaded"', 'id="source-filter-empty"'):
        assert marker in page


def test_selection_mode_and_bulk_lifecycle_controls():
    page = render([profile("field-guide", "Field guide")])
    for marker in ('id="source-select-mode" aria-pressed="false"',
                   'class="group-select" hidden', 'id="bulk-selection" hidden',
                   'id="bulk-actions"', 'id="bulk-publish"',
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
    assert '<section class="source-filters" id="source-filters-panel" aria-label="Filter sources" hidden>' in page
    controls = ("source-filter-search", "source-filter-toggle", "source-filters-panel",
                "source-selection-bar", "source-select-mode", "source-filter-count",
                "bulk-selection", "group-panel", "source-cards")
    positions = [page.index(f'id="{control}"') for control in controls]
    assert positions == sorted(positions)
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
    assert "else if (Number.isInteger(result.revision))" in page
    assert "currentRevision = result.revision" in page
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
        assert 'data-source-artifact="bytes">4096</dd>' in page
        assert '<div hidden><dt>Author:' in page
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
    primary, more = card.split('<details class="source-more-actions">', 1)
    assert primary.count('class="source-actions"') == 1
    assert "Remove source" not in primary
    assert '<summary class="button">More actions</summary>' in more
    assert "Delete downloaded files" in more
    assert "Remove source" in more
    assert "Download from the configured HTTPS URL or upload a PDF from your computer." not in card
    assert "Candidate " not in card
    assert 'id="source-form-busy"' in page
    assert 'data-pdf-busy hidden' in card
    assert "if (formPosting) return;" in page
    assert "Acquisition outcome uncertain" in page
    assert "cancelSave.disabled = false;" in page


def test_published_zim_card_hides_routine_notices():
    pin = WIKIPEDIA_SOURCE
    wiki = profile("wikipedia-en", format="zim", source_url=pin["source_url"],
                   expected_size_bytes=str(pin["bytes"]), sha256=pin["sha256"])
    wiki["publication"] = dict(enabled=True, published=True, verified=True)
    page = render([wiki])
    card = page.split('<div class="source-expanded">', 1)[1].split('</details></div>', 1)[0]
    assert "acquisition disabled pending storage safety integration" not in card
    assert "Unpublish before changing artifact configuration." not in card
    assert ">Unpublish</button>" in card
    wiki["mutation_blocked"] = True
    assert "Acquisition or publication status unresolved; changes are blocked." not in render([wiki])


def test_zim_download_feedback_is_local_and_does_not_reload():
    page = render([profile("ready", format="zim", source_url="https://download.kiwix.org/zim/zimit/ready.zim")])
    card = page.split('data-source-id="ready"', 1)[1].split('</details></div>', 1)[0]
    assert 'data-zim-download' in card
    assert 'data-zim-feedback role="status" aria-live="polite"' in card
    handler = page.split("document.querySelectorAll('[data-zim-download]').forEach", 1)[1].split("document.querySelectorAll('[data-field=", 1)[0]
    assert "Starting download" in handler
    assert "localFeedback.textContent = error.message" in handler
    assert "reloadSources()" not in handler


def test_mobile_download_progress_does_not_crush_title():
    page = render([profile("ready", title="FEMA / Ready.gov", format="zim",
                           source_url="https://download.kiwix.org/zim/zimit/ready.gov_en_all_2026-10.zim")])
    assert 'data-field="summary-progress"' in page
    # The progress bar must consume its own row rather than shrinking the title.
    assert ".source-card > summary > div { flex:1 1 calc(100% - 2.5rem); min-width:0; }" in page
    assert ".source-card > summary .summary-progress { flex:1 1 100%; max-width:none;" in page
    assert ".source-card > summary .badge { margin-left:2.4rem;" in page


def test_add_source_document_upload_sends_session_csrf_header():
    """New-source browser uploads need the same CSRF protection as card uploads."""
    page = render([profile()])
    add_handler = page.split("form.addEventListener('submit', async event => {", 1)[1].split("document.querySelectorAll('[data-pdf-download]", 1)[0]
    add_upload = add_handler.split("const headers = {", 1)[1].split("const response = await acquisitionFetch('/api/pdf-upload'", 1)[0]
    assert "...window.axiomCsrfHeaders()" in add_upload
    assert "'X-PDF-Add':'1'" in add_upload
    # Existing source card imports must also retain the CSRF header.
    card_upload = page.split("? await acquisitionFetch('/api/pdf-upload'", 1)[1]
    assert "window.axiomCsrfHeaders?.()" in card_upload.split("body:file", 1)[0]


def test_retained_zim_offline_revalidation_is_explicit_and_gated():
    page = render([profile("retained-guide", "Retained guide", format="zim",
                           source_url="https://example.org/guide.zim")])
    assert 'data-zim-control="revalidate" hidden' in page
    assert "Recheck saved file (offline)" in page
    assert "action:'zim-' + operation" in page
    assert "operation === 'revalidate'" in page
    assert "No network download will occur." in page
    assert "canRevalidate = recoverable && complete" in page
    assert "source?.acquisition?.retained === true" in page
    assert "Number.isSafeInteger(expected)" in page


def test_zim_stop_restart_retry_controls_are_context_sensitive():
    page = render([profile("small-zim", "Small ZIM", format="zim",
                           source_url="https://example.org/small.zim")])
    parser = Parser()
    parser.feed(page)
    controls = {attrs["data-zim-control"]: attrs for tag, attrs in parser.tags
                if tag == "button" and "data-zim-control" in attrs}
    assert {"pause", "resume", "revalidate", "cancel", "cancel-keep",
            "restart", "retry"} <= controls.keys()
    assert all("hidden" in controls[action] for action in ("cancel", "cancel-keep", "restart", "retry"))
    assert "confirm_restart:true" in page
    assert "operation === 'restart'" in page
    assert "operation === 'cancel-keep'" in page
    assert "retries < 3" in page
    assert "canRestart = ['paused','interrupted','failed','cancelled'].includes(state) && !hasPartial" in page
    assert "permanently discard the partial download" in page


def test_zim_replacement_controls_render_with_explicit_confirmation():
    parser = Parser()
    parser.feed(render([profile("replacement", format="zim",
                                source_url="https://example.org/archive.zim")]))
    actions = [attrs for tag, attrs in parser.tags
               if tag == "button" and "data-zim-replacement" in attrs]
    assert {attrs["data-zim-replacement"] for attrs in actions} == {
        "prepare", "download", "restart", "cancel", "activate", "rollback", "discard"}
    assert all("hidden" in attrs for attrs in actions)
    page = render([profile("replacement", format="zim",
                           source_url="https://example.org/archive.zim")])
    assert "confirm_restart:true" in page
    assert "action:'zim-replacement-' + operation" in page
    assert "Restart update from zero?" in page
    assert "Retry update download" in page


def test_pdf_card_has_no_zim_replacement_controls():
    parser = Parser()
    parser.feed(render([profile("document", format="pdf")]))
    assert not [attrs for tag, attrs in parser.tags
                if tag == "button" and "data-zim-replacement" in attrs]


def test_zim_replacement_preparation_uses_server_eligibility():
    page = render([profile("replacement", format="zim",
                           source_url="https://example.org/new.zim")])
    assert "!source?.replacement_available || !!replacement" in page
    assert "Update verified and ready to publish" in page
    assert "data-zim-replacement-status" in page


def test_replacement_controls_are_independent_of_old_acquisition_status():
    page = render([profile("replacement", format="zim",
                           source_url="https://example.org/new.zim")])
    assert "op !== 'cancel' && blocked && source?.acquisition?.state !== 'completed'" not in page
    assert "op === 'prepare' && !source?.replacement_available" in page
    assert "op === 'download' && !['prepared','failed','interrupted','cancelled'].includes(replacementState)" in page


def test_failed_replacement_discard_requires_safe_retirement_flag():
    page = render([profile("replacement", format="zim",
                           source_url="https://example.org/new.zim")])
    assert "replacement?.can_discard !== true" in page
    assert "['failed','cancelled','interrupted'].includes(replacementState)" in page


def test_installed_url_sources_share_the_update_label():
    url = "https://publisher.org/guide.pdf"
    for kind in ("pdf", "epub", "zim"):
        if kind == "zim":
            source = profile("wikipedia-en", format=kind, source_url=WIKIPEDIA_SOURCE["source_url"])
        else:
            source = profile(format=kind, source_url=url)
        source["installed_verified"] = True
        page = render([source])
        assert ">Check for updates</button>" in page
        assert "Check publisher metadata" not in page

    pending = render([profile(source_url=url)])
    assert "data-document-update-check hidden disabled>Check for updates" in pending
    upload = profile()
    upload["installed_verified"] = True
    assert "data-document-update-check>Check for updates" not in render([upload])


def test_source_icon_is_shared_and_uses_only_local_image_data():
    icon = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII="
    source = profile(title="Reference Library")
    assert '<span aria-hidden="true">RL</span>' in render([source])
    source["profile"]["icon_png"] = icon
    admin = render([source])
    env = Environment(loader=FileSystemLoader(TEMPLATE.parent), autoescape=True)
    public = env.get_template("library.html.j2").render(sources=[dict(
        name="Reference Library", entry_path="/reader", icon_png=icon, category="PDF")])
    for page in (admin, public):
        assert f'<img src="data:image/png;base64,{icon}" alt="">' in page


def test_preconfigured_source_artwork_and_custom_override():
    wiki = profile("wikipedia-en", title="Wikipedia English")
    wiki["profile"]["icon_png"] = WIKIPEDIA_SOURCE["icon_png"]
    assert '<img src="data:image/png;base64,' in render([wiki])
    wiki["profile"]["icon_png"] = "custom-fixture"
    assert '<img src="data:image/png;base64,custom-fixture" alt="">' in render([wiki])
    assert '🚦' in render([profile("custom-rail", title="railroad signals")])


def test_zim_update_status_precedes_the_source_buttons():
    page = render([profile(format="zim", source_url="https://example.org/fixture.zim")])
    status = page.index("data-zim-replacement-status role=")
    actions = page.index('aria-label="Source actions"')
    publish = page.index('data-zim-replacement="activate" hidden>Publish update')
    assert status < actions < publish
