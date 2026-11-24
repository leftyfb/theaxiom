"""Chromium browser regression against the real admin HTTP handler.

Runs on isolated loopback with temporary catalog, credentials and configuration.
Never connects to Vancouver or opens a production archive.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

from theaxiom import admin_api, admin_sessions, catalog_store
from theaxiom.admin_security import PasswordStore, ServerConfig


def exercise(browser, base, name, viewport, artifacts):
    context = browser.new_context(viewport=viewport, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(
        f"{page.url}: {error}\\n{getattr(error, 'stack', '')}"))
    try:
        # Test the application through real browser requests, not mock HTML.
        page.goto(base + "/")
        expect(page.locator("body")).to_contain_text("offline")
        page.goto(base + "/search?q=emergency")
        expect(page.locator("body")).to_be_visible()
        # Public pages are the primary mobile entry points. Protect their
        # layout independently of the authenticated administrator pages.
        for public_path in ("/", "/search?q=emergency"):
            page.goto(base + public_path)
            public_overflow = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth
            })""")
            assert public_overflow["scroll"] <= public_overflow["viewport"] + 2, (
                f"{name} {public_path} public horizontal overflow: {public_overflow}")

        def search_results(query, sources, *, page, start_base):
            first = (page - 1) * 20 + 1
            return [
                dict(source_id="matching", name="Matching source", status="ok",
                     page=page, has_more=page == 1, start_base=start_base,
                     results=[dict(title=f"Water result {i}", href=f"/fixture/article/{i}", snippet="Water")
                              for i in range(first, min(first + 20, 23))]),
                dict(source_id="no-text", name="Image-only document", status="no_text", results=[]),
                dict(source_id="no-match", name="Unrelated source", status="ok", results=[]),
            ]

        with patch.object(admin_api.AdminAPIHandler, "_search_published_content", side_effect=search_results):
            page.goto(base + "/search?q=water")
            section = page.get_by_role("region", name="Matching source")
            expect(section.get_by_role("listitem")).to_have_count(20)
            expect(section.get_by_role("link", name="Water result 20")).to_be_visible()
            expect(section.locator("summary")).to_have_count(0)
            expect(page.get_by_text("Image-only document")).to_have_count(0)
            expect(page.get_by_text("Unrelated source")).to_have_count(0)
            section.get_by_role("link", name="Next page").click()
            expect(section.get_by_role("listitem")).to_have_count(2)
            expect(section.get_by_role("link", name="Water result 21")).to_be_visible()
            expect(section.get_by_role("link", name="Next page")).to_have_count(0)
            section.get_by_role("link", name="Previous page").click()
            expect(section.get_by_role("link", name="Water result 20")).to_be_visible()

        # Browser navigation must not bypass the sign-in boundary.
        page.goto(base + "/admin/sources")
        expect(page).to_have_url(base + "/admin/login")
        page.locator("#username").fill("admin")
        page.locator("#password").fill("wrong-password")
        page.locator("#submit").click()
        expect(page.locator("#feedback")).to_contain_text("Invalid")
        # An invalid login must not establish a session, even if the user
        # navigates directly to another protected route in the same tab.
        page.goto(base + "/admin/settings")
        expect(page).to_have_url(base + "/admin/login")
        page.locator("#username").fill("admin")
        page.locator("#password").fill("browser-only-fixture-password")
        page.locator("#submit").click()
        expect(page).to_have_url(base + "/admin")
        expect(page.locator("h1")).to_contain_text("Manage offline sources")
        # The settings page polls storage status. A local import inside
        # do_GET previously shadowed WIKIPEDIA_SOURCE and crashed this API.
        storage_status = context.request.get(base + "/api/storage-status")
        assert storage_status.status in (200, 503), (
            name, "storage status endpoint crashed", storage_status.status)
        assert isinstance(storage_status.json(), dict)

        # An authenticated browser context must not authorize a separate
        # browser context. This catches accidental global auth state and
        # cookie leakage in the real HTTP handler (no mock responses).
        isolated = browser.new_context(viewport=viewport)
        try:
            outsider = isolated.new_page()
            for protected in ("/admin", "/admin/sources", "/admin/groupings", "/admin/settings"):
                outsider.goto(base + protected)
                expect(outsider).to_have_url(base + "/admin/login")
            outsider.goto(base + "/")
            expect(outsider.locator("body")).to_be_visible()
            # The original authenticated context must remain signed in.
            page.goto(base + "/admin")
            expect(page.locator("h1")).to_contain_text("Manage offline sources")
        finally:
            isolated.close()

        # A real modal and its controls need to be operable at both widths.
        guide_trigger = page.locator("#source-guide-open")
        guide_trigger.focus()
        expect(guide_trigger).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#source-guide")).to_be_hidden()
        # Opening the guide a second time must work after an Escape dismissal.
        guide_trigger.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#source-guide")).to_be_hidden()
        # Dismissed guidance must not trap subsequent keyboard navigation.
        guide_trigger.focus()
        expect(guide_trigger).to_be_focused()

        filter_toggle = page.locator("#source-filter-toggle")
        filter_toggle.focus()
        expect(filter_toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-filters-panel")).to_be_visible()
        expect(page.locator("#source-filter-toggle")).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#source-filter-search")).to_be_visible()
        filter_toggle.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#source-filters-panel")).to_be_hidden()
        expect(filter_toggle).to_have_attribute("aria-expanded", "false")

        # Exercise live JS form submission, dynamic reload, search and edit.
        add_source = page.locator("#source-add")
        add_source.focus()
        expect(add_source).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-dialog")).to_be_visible()
        title = "Browser Regression " + name
        sid = "browser-regression-" + name
        page.locator("#catalog-title").fill(title)
        page.locator("#catalog-format").select_option("pdf")
        page.locator("#catalog-url").fill("https://publisher.org/download.pdf")
        page.locator("#catalog-update-method").select_option("head")
        page.locator("#catalog-update-endpoint").fill("https://publisher.org/metadata")
        page.locator("#catalog-update-periodic").select_option("yes")
        with page.expect_response(lambda r: r.url.endswith("/api/update-endpoint-test") and r.status == 200):
            page.locator("#test-update-endpoint").click()
        expect(page.locator("#update-endpoint-feedback")).to_contain_text("strong ETag")
        page.locator("#catalog-update-endpoint").fill("https://127.0.0.1/private")
        with page.expect_response(lambda r: r.url.endswith("/api/update-endpoint-test") and r.status == 400):
            page.locator("#test-update-endpoint").click()
        expect(page.locator("#update-endpoint-feedback")).to_contain_text("plain HTTPS")
        page.locator("#catalog-update-endpoint").fill("https://publisher.org/metadata")
        # The application intentionally reloads after a catalog mutation.
        # Wait for that navigation, not just an already-present DOM element.
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('#source-form [type="submit"]').click()
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible(timeout=15000)

        search = page.locator("#source-filter-search")
        search.fill("not-a-match-" + name)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_hidden()
        # Case-insensitive search and whitespace handling are important for
        # source discovery on narrow/mobile screens.
        search.fill(title.upper())
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        search.fill(title)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        search.fill("")

        # Bulk-selection state and keyboard access: no action is executed.
        selection_toggle = page.locator("#source-select-mode")
        selection_toggle.focus()
        expect(selection_toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(selection_toggle).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#group-select-all")).to_be_visible()
        selection_toggle.focus()
        page.keyboard.press("Enter")
        expect(selection_toggle).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#group-select-all")).to_be_hidden()

        card = page.locator(".source-card[data-source-id='" + sid + "']")
        card.locator("summary.source-header").click()
        edit_source = card.locator("[data-catalog-edit]")
        edit_source.focus()
        expect(edit_source).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-dialog")).to_be_visible()
        expect(page.locator("#catalog-title")).to_have_value(title)
        expect(page.locator("#catalog-url")).to_have_value("https://publisher.org/download.pdf")
        expect(page.locator("#catalog-update-endpoint")).to_have_value("https://publisher.org/metadata")
        expect(page.locator("#catalog-update-method")).to_have_value("head")
        expect(page.locator("#catalog-update-periodic")).to_have_value("yes")
        icon_file = page.locator("#source-icon-file")
        icon_file.set_input_files(str(Path(__file__).resolve().parents[1] / "templates" / "favicon.png"))
        expect(page.locator("#source-icon-preview img")).to_be_visible()
        page.locator("#catalog-description").fill("Verified through real browser UI")
        # The application intentionally reloads after a catalog mutation.
        # Wait for that navigation, not just an already-present DOM element.
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('#source-form [type="submit"]').click()
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible(timeout=15000)

        expect(card.locator(".source-symbol img")).to_be_visible()
        if card.get_attribute("open") is None:
            card.locator("summary.source-header").click()
        card.locator("[data-catalog-edit]").click()
        page.locator("#source-icon-remove").click()
        expect(page.locator("#source-icon-preview img")).to_have_count(0)
        page.locator("#source-cancel").click()
        expect(card.locator(".source-symbol img")).to_be_visible()
        card.locator("[data-catalog-edit]").click()
        page.locator("#source-icon-remove").click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('#source-form [type="submit"]').click()
        expect(card.locator(".source-symbol img")).to_have_count(0)

        # Closed #68/#70/#77: browser-provided filename populates a new
        # document's title and format. Cancel must not persist the candidate.
        page.locator("#source-add").click()
        page.locator("#source-upload-file").set_input_files({
            "name": "Offline_Guide.epub",
            "mimeType": "application/epub+zip",
            "buffer": b"disposable metadata-only fixture",
        })
        expect(page.locator("#catalog-title")).to_have_value("Offline Guide")
        expect(page.locator("#catalog-format")).to_have_value("epub")
        # The canceled browser upload must not create a catalog record or
        # retain the selected file when the dialog is reopened.
        assert page.locator(".source-card[data-source-id='offline-guide']").count() == 0
        cancel_source = page.locator("#source-cancel")
        cancel_source.focus()
        expect(cancel_source).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-dialog")).to_be_hidden()
        assert page.locator(".source-card[data-source-id='offline-guide']").count() == 0
        page.locator("#source-add").click()
        expect(page.locator("#source-dialog")).to_be_visible()
        expect(page.locator("#source-upload-file")).to_have_value("")
        page.locator("#source-cancel").click()

        # Closed #78: independent search/format/publication/download filters,
        # reset, counts and stable alphabetical sorting without duplicates.
        page.locator("#source-filter-toggle").click()
        page.locator("#source-filter-format").select_option("pdf")
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        page.locator("#source-filter-download").select_option("downloaded")
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_hidden()
        page.locator("#source-filter-download").select_option("")
        page.locator("#source-filter-publication").select_option("published")
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_hidden()
        reset_filters = page.locator("#source-filter-reset")
        reset_filters.focus()
        expect(reset_filters).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        expect(page.locator("#source-filter-format")).to_have_value("")
        expect(page.locator("#source-filter-download")).to_have_value("")
        expect(page.locator("#source-filter-publication")).to_have_value("")
        expect(page.locator("#source-filter-search")).to_have_value("")
        page.locator("#source-filter-sort").select_option("alpha")
        names = page.locator("#source-cards .source-row:not([hidden]) .source-card").evaluate_all(
            "(cards) => cards.map(card => card.dataset.filterTitle.toLocaleLowerCase())")
        assert names == sorted(names), f"{name} alphabetical source order regression"
        ids = page.locator("#source-cards .source-card").evaluate_all(
            "(cards) => cards.map(card => card.dataset.sourceId)")
        assert len(ids) == len(set(ids)), "source cards duplicated during sort"
        page.locator("#source-filter-reset").click()
        expect(page.locator("#source-filter-sort")).not_to_have_value("alpha")
        page.locator("#source-filter-toggle").click()

        # Closed #71: selection checkboxes are opt-in and bulk publication
        # remains gated for the unverified disposable source.
        page.locator("#source-select-mode").click()
        check = page.locator('[data-group-select][value="' + sid + '"]')
        check.focus()
        expect(check).to_be_focused()
        page.keyboard.press("Space")
        expect(check).to_be_checked()
        expect(page.locator("#group-count")).to_contain_text("1 selected")
        expect(page.locator("#bulk-publish")).to_be_disabled()
        expect(page.locator("#bulk-unpublish")).to_be_disabled()
        page.get_by_role("button", name="Batch download").click()
        expect(page.locator("#bulk-preview-summary")).to_contain_text("1 download")
        expect(page.locator("#bulk-preview-capacity")).to_have_text("Current storage capacity is unavailable.")

        def bounded_preview(route):
            response = route.fetch().json()
            response["capacity"] = {"state": "fits_with_unknown_sizes", "available_bytes": 33554432,
                                    "reserve_bytes": 0, "staging_upper_bound_bytes": 16777216}
            response["sources"][0].update(installed_bytes=1000, peak_bytes=1000000000)
            route.fulfill(json=response)

        page.get_by_role("button", name="Batch download").click()  # Collapse the first preview.
        page.route("**/api/acquisition-preview", bounded_preview)
        page.get_by_role("button", name="Batch download").click()
        expect(page.locator("#bulk-preview-capacity")).to_contain_text("Enough space")
        expect(page.locator("#bulk-preview-sources")).to_be_hidden()
        page.get_by_text("Storage details", exact=True).click()
        expect(page.locator("#bulk-preview-storage")).to_contain_text("Staging could use up to 16.78 MB")
        expect(page.locator("#bulk-preview-sources")).to_contain_text("Installed 1 kB")
        expect(page.locator("#bulk-preview-sources")).to_contain_text("Peak 1 GB")
        page.unroute("**/api/acquisition-preview", bounded_preview)
        batch_jobs = []
        job_id = "a" * 32

        def batch_downloads(route):
            if route.request.method == "GET":
                route.fulfill(json={"jobs": batch_jobs})
                return
            payload = route.request.post_data_json
            if payload.get("cancel") == job_id:
                batch_jobs[0]["state"] = "cancelled"
                route.fulfill(status=202, json={"accepted": True})
                return
            assert payload["confirm"] is True and payload["source_ids"] == [sid]
            batch_jobs.append(dict(id=job_id, source_id=sid, title="Fixture download",
                                   bytes=16777216, state="queued", stop=0, detail=""))
            route.fulfill(status=202, json={"job_ids": [job_id]})

        page.route("**/api/acquisition-batch", batch_downloads)
        page.get_by_role("button", name="Start downloads").click()
        queued_card = page.locator(f'.source-card[data-source-id="{sid}"]')
        expect(page.get_by_text("Batch downloads", exact=True)).to_have_count(0)
        expect(queued_card.locator("[data-batch-status]")).to_have_text("Waiting to download")
        if not queued_card.evaluate("(card) => card.open"):
            queued_card.locator(".source-header").click()
        queued_card.get_by_role("button", name="Cancel queued download").click()
        expect(queued_card.locator("[data-batch-status]")).to_have_text("Batch download cancelled")
        batch_jobs[0].update(state="completed", id="b" * 32)
        expect(queued_card.locator("[data-batch-status]")).to_be_hidden()
        page.unroute("**/api/acquisition-batch", batch_downloads)
        def stale_sources(route):
            response = route.fetch().json()
            response["revision"] += 1
            route.fulfill(json=response)

        page.route("**/api/sources", stale_sources, times=1)
        expect(page.get_by_role("button", name="Reload sources")).to_be_visible(timeout=10000)
        page.get_by_role("button", name="Reload sources").click()
        expect(page.locator('[data-group-select][value="' + sid + '"]')).to_be_checked()
        page.locator("#bulk-more-actions summary").click()
        page.get_by_role("button", name="Export ZIM verification manifest").click()
        expect(page.locator("#catalog-feedback")).to_contain_text("Choose only installed, verified ZIM sources")
        with page.expect_download() as download:
            page.get_by_role("button", name="Export selected sources").click()
        manifest = download.value.path().read_bytes()
        check.uncheck()
        page.get_by_role("button", name="Presets and saved selections").click()
        page.get_by_label("Starter preset").select_option("medical")
        expect(page.locator("#selection-file-matches")).to_contain_text("CDC")
        expect(page.get_by_role("button", name="Select matching sources")).to_be_enabled()
        transfer = {"kind": "axiom-local-transfer", "version": 1, "artifacts": [{
            "source_id": sid, "format": "zim", "title": "Portable reference", "language": "eng",
            "edition": "2026-01", "bytes": 4096, "filename": "reference.zim",
            "sha256": "a" * 64, "license": "Synthetic fixture", "provenance": "",
        }]}
        page.locator("#selection-file").set_input_files({
            "name": "transfer.json", "mimeType": "application/json", "buffer": json.dumps(transfer).encode(),
        })
        expect(page.locator("#selection-file-matches")).to_contain_text("Local source ID already configured")
        expect(page.get_by_role("button", name="Select matching sources")).to_be_disabled()
        page.locator("#selection-file").set_input_files({
            "name": "selection.json", "mimeType": "application/json", "buffer": manifest,
        })
        expect(page.locator("#selection-file-matches")).to_contain_text("Matches this catalog · PDF")
        expect(page.locator("#selection-storage-summary")).to_contain_text("Installed unknown")
        mismatch = json.loads(manifest)
        mismatch["sources"][0]["edition"] = "2025"
        page.locator("#selection-file").set_input_files({
            "name": "other-edition.json", "mimeType": "application/json", "buffer": json.dumps(mismatch).encode(),
        })
        expect(page.locator("#selection-file-matches")).to_contain_text("Requested PDF / 2025")
        expect(page.locator("#selection-file-matches")).to_contain_text("Local PDF")
        expect(page.get_by_role("button", name="Select matching sources")).to_be_disabled()
        page.locator("#selection-file").set_input_files({
            "name": "selection.json", "mimeType": "application/json", "buffer": manifest,
        })
        expect(page.get_by_role("button", name="Select matching sources")).to_be_enabled()
        expect(check).not_to_be_checked()
        page.get_by_role("button", name="Select matching sources").click()
        expect(check).to_be_checked()
        expect(page.get_by_role("button", name="Browse Kiwix catalog")).to_have_count(0)
        check.focus()
        page.keyboard.press("Space")
        expect(check).not_to_be_checked()
        expect(page.locator("#bulk-selection")).to_be_hidden()
        expect(page.locator("#bulk-preview-result")).to_be_hidden()
        page.locator("#source-select-mode").click()

        # Closed #3/#76: explicit cancel leaves source intact, confirmed
        # removal deletes ONLY the test catalog entry (no artifact exists).
        card = page.locator(".source-card[data-source-id='" + sid + "']")
        # Edit/save preserves expanded cards across reloads. Do not collapse
        # an already-open card before interacting with its Remove button.
        if not card.evaluate("(element) => element.open"):
            card.locator("summary.source-header").click()
        expect(card.locator("[data-catalog-remove]")).to_be_hidden()
        more_actions = card.get_by_text("More actions", exact=True)
        menu_box = more_actions.bounding_box()
        edit_box = card.locator("[data-catalog-edit]").bounding_box()
        assert menu_box and edit_box
        assert abs(menu_box["height"] - edit_box["height"]) < 1
        if name == "desktop":
            assert menu_box["height"] == 36
            assert abs(menu_box["y"] - edit_box["y"]) < 1
        more_actions.click()
        expect(card.get_by_role("button", name="Remove source")).to_be_visible()
        remove_source = card.locator("[data-catalog-remove]")
        remove_source.focus()
        expect(remove_source).to_be_focused()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.keyboard.press("Enter")
        expect(card).to_be_visible()
        # Dismissing the native confirmation must preserve the catalog entry
        # even after a fresh server response, not only in the current DOM.
        page.reload()
        card = page.locator(".source-card[data-source-id='" + sid + "']")
        expect(card).to_be_visible()
        if not card.evaluate("(element) => element.open"):
            card.locator("summary.source-header").click()
        card.get_by_text("More actions", exact=True).click()
        remove_source = card.locator("[data-catalog-remove]")
        remove_source.focus()
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.keyboard.press("Enter")
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_have_count(0)
        page.reload()
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_have_count(0)

        # Closed #71: manage an ephemeral collection in its own namespace.
        page.goto(base + "/admin/groupings")
        # #52: Keyboard reorder, persist the preference across reload, then reset.
        layout_rows = page.locator("#layout-sources > li")
        old_order = layout_rows.evaluate_all("(items) => items.map(item => item.dataset.layoutSource)")
        assert len(old_order) >= 2
        move_up = layout_rows.nth(1).locator('[data-layout-move="-1"]')
        move_up.focus()
        page.keyboard.press("Enter")
        expect(layout_rows.first).to_have_attribute("data-layout-source", old_order[1])
        expect(layout_rows.first.locator('[data-layout-move="1"]')).to_be_focused()
        page.locator("#layout-group-by").select_option("collections")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("button", name="Save library layout").click()
        expect(layout_rows.first).to_have_attribute("data-layout-source", old_order[1])
        expect(page.locator("#layout-group-by")).to_have_value("collections")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("button", name="Reset to default").click()
        expect(layout_rows.first).to_have_attribute("data-layout-source", old_order[0])
        expect(page.locator("#layout-group-by")).to_have_value("none")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
        group_name = "Browser Group " + name
        renamed = "Browser Renamed " + name
        page.locator("#manage-kind").select_option("collections")
        page.locator("#manage-new").fill(group_name)
        create_group = page.locator("#manage-create")
        create_group.focus()
        expect(create_group).to_be_focused()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.keyboard.press("Enter")
        page.locator("#manage-kind").select_option("collections")
        row_input = page.get_by_role("textbox", name="Name of " + group_name)
        expect(row_input).to_be_visible()
        row_input.fill(renamed)
        row = row_input.locator("xpath=..").locator("xpath=..")
        rename_group = row.get_by_role("button", name="Rename")
        rename_group.focus()
        expect(rename_group).to_be_focused()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.keyboard.press("Enter")
        page.locator("#manage-kind").select_option("collections")
        row_input = page.get_by_role("textbox", name="Name of " + renamed)
        expect(row_input).to_have_value(renamed)
        row = row_input.locator("xpath=..").locator("xpath=..")
        delete_group = row.get_by_role("button", name="Delete")
        delete_group.focus()
        expect(delete_group).to_be_focused()
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.keyboard.press("Enter")
        page.locator("#manage-kind").select_option("collections")
        expect(page.get_by_role("textbox", name="Name of " + renamed)).to_have_count(0)

        # Mobile usability guard: horizontal viewport overflow breaks controls.
        page.goto(base + "/admin/groupings")
        expect(page.locator("h1")).to_be_visible()
        page.goto(base + "/admin/settings")
        expect(page.locator("h1")).to_be_visible()
        for path in ("/admin", "/admin/sources", "/admin/groupings", "/admin/settings"):
            page.goto(base + path)
            overflow = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth
            })""")
            assert overflow["scroll"] <= overflow["viewport"] + 2, (
                f"{name} {path} horizontal overflow: {overflow}")

        # Two tabs in the same browser context share one session. Signing
        # out in either tab must revoke access in the other, not merely
        # redirect the tab that clicked Sign out.
        sibling_tab = context.new_page()
        sibling_tab.goto(base + "/admin")
        expect(sibling_tab.locator("h1")).to_contain_text("Manage offline sources")

        # Logout must revoke the session and redirect back to sign-in.
        logout = page.locator("#admin-logout")
        if logout.count():
            logout.click()
            expect(page).to_have_url(base + "/admin/login")
        else:
            page.goto(base + "/admin")
            logout = page.get_by_role("button", name="Sign out")
            expect(logout).to_be_visible()
            logout.click()
            expect(page).to_have_url(base + "/admin/login")
        sibling_tab.goto(base + "/admin/settings")
        expect(sibling_tab).to_have_url(base + "/admin/login")
        sibling_tab.close()
        # Browser history and direct URL navigation must not restore a
        # revoked admin session after logout (closed authentication issues).
        page.goto(base + "/admin/sources")
        expect(page).to_have_url(base + "/admin/login")
        # Back/forward navigation must not authorize subsequent requests.
        page.go_back()
        page.goto(base + "/admin/settings")
        expect(page).to_have_url(base + "/admin/login")
        # #99: The Sources background poll must recover from a revoked/expired
        # session via the branded login, without requiring a browser restart.
        # Clear only the isolated in-memory fixture sessions: no production
        # credentials, archives or stored source profiles are touched.
        page.goto(base + "/admin/login")
        page.locator("#username").fill("admin")
        page.locator("#password").fill("browser-only-fixture-password")
        page.locator("#submit").click()
        expect(page).to_have_url(base + "/admin")
        page.goto(base + "/admin/sources")
        expect(page.locator("h1")).to_contain_text("Manage offline sources")
        admin_api.ADMIN_SESSIONS.clear()
        # The real 2-second Sources poll detects the 401 and redirects;
        # an HTTP Basic challenge would instead halt this navigation.
        expect(page).to_have_url(base + "/admin/login", timeout=12000)
        expect(page.locator("#username")).to_be_visible()
        page.go_back()
        # A cached admin document may briefly reappear via history, but
        # subsequent navigation must still enforce the revoked session.
        page.goto(base + "/admin/groupings")
        expect(page).to_have_url(base + "/admin/login")
        assert not errors, f"{name} uncaught browser JavaScript errors: {errors}"
        print(f"PASS {name}: login, navigation, guide, filters, add/edit, selection, overflow, logout")
    except Exception:
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f"{name}-failure.png"), full_page=True)
        (artifacts / f"{name}-pageerrors.txt").write_text("\n".join(errors) or "(none)")
        raise
    finally:
        context.close()


def exercise_history_poll(browser, viewport):
    """Use the real source-card DOM and poller with two independent timelines."""
    from test_admin_page_template import profile, render

    first = dict(profile("history-first", format="zim", source_url="https://publisher.org/first.zim"),
                 publication={}, acquisition=None, installed_verified=True, artifact={"bytes": 2097152})
    second = dict(profile("history-second", format="zim", source_url="https://publisher.org/second.zim"),
                  publication={}, acquisition=None)
    page = browser.new_page(viewport=viewport)
    changed = False

    def poll(route):
        update = ({"state": "unable_to_determine", "last_known_candidate_edition": "2026-11", "stale": True}
                  if changed else {"state": "update_available", "installed_edition": "2026-10",
                                   "candidate_edition": "2026-11", "candidate_bytes": 1048576})
        sources = [dict(first, update_check=update, acquisition={
            "state": "failed" if changed else "downloading",
            "attempt_history": [
                {"state": "completed", "finished_at": 1700000000},
                *([{"state": "failed", "finished_at": 1700000100}] if changed else []),
            ],
            "last_successful_acquisition": 1700000000,
        }), dict(second, acquisition={
            "state": "completed" if changed else "paused",
            "attempt_history": [{"state": "completed" if changed else "paused",
                                 "finished_at": 1700000300}],
        })]
        route.fulfill(json={"sources": sources, "revision": 0, "acquisition_stale": False})

    try:
        page.route("http://fixture.local/admin/sources", lambda route: route.fulfill(
            content_type="text/html", body=render([first, second])))
        page.route("http://fixture.local/api/sources", poll)
        page.goto("http://fixture.local/admin/sources")
        card = page.locator('[data-source-id="history-first"]')
        other = page.locator('[data-source-id="history-second"]')
        card.locator("summary.source-header").click()
        technical = card.locator(".source-technical")
        summary = technical.locator("summary")
        summary.focus()
        page.keyboard.press("Enter")
        expect(technical.locator('[data-field="acquisition-history"]')).to_contain_text("Succeeded")
        expect(technical.locator('[data-field="last-acquisition"]')).not_to_have_text("—")
        details = technical.locator('[data-field="update-details"]')
        expect(details).to_contain_text("Estimated transfer and staging: 1.05 MB")
        expect(details).to_contain_text("Installed and candidate archives together: 3.15 MB")
        changed = True
        expect(technical.locator('[data-field="acquisition-history"]')).to_contain_text("Failed")
        expect(details).to_be_hidden()
        expect(technical.locator('[data-field="update-evidence"]')).to_contain_text("Last known candidate 2026-11")
        expect(other.locator('[data-field="status"]')).to_have_text("Acquisition completed")
        expect(card).to_have_attribute("open", "")
        expect(technical).to_have_attribute("open", "")
        expect(summary).to_be_focused()
        assert not other.evaluate("(card) => card.open")
    finally:
        page.close()



def exercise_source_actions(browser, viewport):
    """Use controlled responses to exercise the actual rendered source cards."""
    from test_admin_page_template import profile, render

    fillers = [dict(profile(f"fixture-{i:02}", title=f"Fixture {i:02}", format="other"),
                    publication_filter="unpublished", download_filter="not-downloaded")
               for i in range(60)]
    target = dict(profile("action-target", title="Fixture 30 PDF", source_url="https://publisher.org/fixture.pdf"),
                  publication={"entry_path": "/fixture.pdf"}, publication_filter="unpublished",
                  download_filter="not-downloaded")
    revision = 0
    downloads = 0

    def api(route):
        nonlocal revision, downloads
        if route.request.method == "GET":
            route.fulfill(json={"revision": revision, "sources": [*fillers, target]})
            return
        payload = route.request.post_data_json
        assert payload["revision"] == revision
        if route.request.url.endswith("/publication"):
            enabled = payload["enabled"]
            target["publication"].update(enabled=enabled, published=enabled)
            target["publication_filter"] = "published" if enabled else "unpublished"
            route.fulfill(json={"enabled": enabled})
            return
        assert payload["action"] == "pdf-download"
        downloads += 1
        if downloads == 2:
            route.fulfill(status=409, json={"error": "Fixture download failed"})
            return
        revision += 1
        target.update(installed_verified=True, download_filter="downloaded",
                      artifact={"bytes": 1024 * revision})
        target["publication"]["ready_for_publication"] = True
        route.fulfill(json={"installed_verified": True, "revision": revision})

    page = browser.new_page(viewport=viewport)
    navigations = []
    errors = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url))
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        markup = render([*fillers, target])
        page.route("http://fixture.local/admin/sources", lambda route: route.fulfill(
            content_type="text/html", body=markup))
        page.route("http://fixture.local/api/*", api)
        page.goto("http://fixture.local/admin/sources")
        card = page.locator('[data-source-id="action-target"]')
        summary = card.locator(".source-header")
        download = card.locator("[data-pdf-download]")
        feedback = card.locator("[data-pdf-feedback]")
        publish = card.locator('[data-field="publication-toggle"]')
        search = page.locator("#source-filter-search")
        sort = page.locator("#source-filter-sort")
        page.locator("#source-filter-toggle").click()
        page.locator("#source-filter-format").select_option("pdf")
        expect(card).to_be_visible()
        page.locator("#source-filter-format").select_option("")
        search.fill("Fixture")
        sort.select_option("alpha")
        page.locator("#source-select-mode").click()
        selected = page.locator('[data-group-select][value="action-target"]')
        selected.check()
        assert summary.evaluate("(node) => node.getBoundingClientRect().top + scrollY") > viewport["height"]
        summary.click()
        download.scroll_into_view_if_needed()
        top = card.evaluate("(node) => node.getBoundingClientRect().top")
        download.focus()
        page.keyboard.press("Enter")
        expect(feedback).to_have_text("PDF downloaded and verified.")
        expect(download).to_have_text("Re-download")
        expect(download).to_be_focused()
        assert abs(card.evaluate("(node) => node.getBoundingClientRect().top") - top) <= 2

        page.keyboard.press("Enter")
        expect(feedback).to_have_text("Fixture download failed")
        expect(download).to_be_focused()
        page.keyboard.press("Enter")
        expect(card.locator('[data-field="status"]')).to_have_text("2.05 kB")
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()
        expect(search).to_have_value("Fixture")
        expect(sort).to_have_value("alpha")

        # Start below the fold in default order. Other downloaded cards keep
        # the target below the fold until publication moves it to the front.
        for source in fillers[:24]:
            source["download_filter"] = "downloaded"
        sort.select_option("status")
        expect(page.locator(".source-card").nth(24)).to_have_attribute("data-source-id", "action-target")
        publish.scroll_into_view_if_needed()
        publish.focus()
        page.keyboard.press("Enter")
        expect(page.locator(".source-card").first).to_have_attribute("data-source-id", "action-target")
        expect(publish).to_have_text("Unpublish")
        expect(publish).to_be_in_viewport()
        expect(publish).to_be_focused()
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()

        page.locator("#source-filter-publication").select_option("published")
        publish.click()
        expect(card).to_be_hidden()
        expect(page.locator("#filtered-action-feedback")).to_have_text("Fixture 30 PDF — Source unpublished.")
        expect(page.locator("#filtered-action-feedback")).to_be_in_viewport()
        page.locator("#source-filter-publication").select_option("")
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()
        assert len(navigations) == 1, navigations
        assert not errors, errors
    finally:
        page.close()


def main():
    artifacts = Path(os.environ.get("AXIOM_BROWSER_ARTIFACTS", "/tmp/axiom-browser-artifacts"))
    with tempfile.TemporaryDirectory(prefix="axiom-browser-fixtures-") as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config"),
                                     "THEAXIOM_STORAGE_SETTINGS": str(root / "storage.json")}):
            with patch.object(admin_api, "SOURCE_CATALOG",
                              catalog_store.CatalogStore(root / "sources.json")):
                with (patch.object(admin_api, "ADMIN_SESSIONS", admin_sessions.Sessions()),
                      patch.object(admin_api.update_discovery_config.pdf_transport,
                                   "probe_document_metadata",
                                   return_value={"reachable": True, "etag": '"fixture"'})):
                    credential = root / "credentials"
                    PasswordStore.initialize(credential, "browser-only-fixture-password")
                    server = admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler)
                    host, port = server.server_address
                    server.axiom_config = ServerConfig(host, port, frozenset({f"{host}:{port}"}),
                                                       "admin", PasswordStore(credential))
                    # Catalog mutations deliberately fail closed when monitoring is stale.
                    # Supply a clean test-only snapshot; do not relax production guards.
                    server.acquisition_monitor = SimpleNamespace(
                        snapshot=lambda: {"stale": False, "acquisitions": []})
                    worker = threading.Thread(target=server.serve_forever, daemon=True)
                    worker.start()
                    try:
                        with sync_playwright() as playwright:
                            browser = playwright.chromium.launch(headless=True, executable_path=os.getenv("AXIOM_TEST_CHROMIUM"))
                            try:
                                for name, viewport in (
                                    ("desktop", {"width": 1366, "height": 900}),
                                    ("mobile", {"width": 390, "height": 844}),
                                    ("narrow", {"width": 320, "height": 640}),
                                ):
                                    exercise(browser, f"http://{host}:{port}", name, viewport, artifacts)
                                    exercise_history_poll(browser, viewport)
                                    exercise_source_actions(browser, viewport)
                            finally:
                                browser.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        worker.join(timeout=5)


if __name__ == "__main__":
    main()
