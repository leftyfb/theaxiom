"""Chromium browser regression against the real admin HTTP handler.

Runs on isolated loopback with temporary catalog, credentials and configuration.
Never connects to Vancouver or opens a production archive.
"""
import json
import os
import re
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

from browser_timing import timed_case
from theaxiom import admin_api, admin_sessions, catalog_store
from theaxiom.admin_security import PasswordStore, ServerConfig


def check_kiwix_article_navigation(page, base):
    book = "wikibooks." + "a" * 64
    viewer = base + "/zim/viewer"
    category = book + "/Category%3ABook%3AKnots"
    bowline = book + "/Knots/Loop_knots/Bowline"
    basic = book + "/Category%3ABook%3AKnots/Basic_knots"
    content = base + "/zim/content/"
    script = Path(__file__).resolve().parents[1] / "templates/kiwix-navigation.js"

    def serve(route):
        url = route.request.url
        if url == viewer:
            body = """<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
                <style>html {height:100%} html,body,#content_iframe {display:flex;flex-direction:column;flex:1}
                body {margin:0} #kiwixtoolbar {padding:8px;transition:0.3s;width:100%;box-sizing:border-box}</style>
                <div id="kiwixtoolbarwrapper"><div id="kiwixtoolbar"><a href="/">Back to library</a></div></div>
                <iframe id="content_iframe" style="border:0" title="ZIM content"></iframe>
                <script>
                  const frame = document.querySelector('iframe');
                  const toolbar = document.querySelector('#kiwixtoolbar');
                  let previousScrollTop = Infinity;
                  // Reproduce the upstream Kiwix mobile auto-hide mechanism.
                  if (document.body.clientWidth < 520) setInterval(() => {
                    const doc = frame.contentDocument;
                    if (!doc?.body) return;
                    const top = doc.documentElement.scrollTop || doc.body.scrollTop;
                    if (Math.abs(previousScrollTop - top) <= 5) return;
                    toolbar.style.position = top > previousScrollTop ? 'fixed' : 'static';
                    toolbar.style.top = top > previousScrollTop ? '-100%' : '0';
                    previousScrollTop = top;
                  }, 250);
                  const load = () => document.querySelector('iframe').contentWindow.location.replace('/zim/content/' + location.hash.slice(1));
                  window.addEventListener('hashchange', load);
                  load();
                </script><script src="/zim/axiom-navigation.js"></script>"""
        elif url == content + category:
            body = (f'<a href="/zim/content/{bowline}"><span>Bowline</span></a>'
                    f'<a href="/zim/viewer#{basic}">Basic knots</a>'
                    '<div style="height:3000px"></div>'
                    '<script>document.addEventListener("click", e => e.preventDefault())</script>')
        elif url == content + bowline:
            body = '<h1>Bowline</h1><div style="height:3000px"></div>'
        elif url == content + basic:
            body = '<h1>Basic knots</h1>'
        elif url == base + "/zim/axiom-navigation.js":
            route.fulfill(content_type="text/javascript", body=script.read_text())
            return
        else:
            route.abort()
            return
        route.fulfill(content_type="text/html", body=body)

    page.route("**/zim/**", serve)
    try:
        page.goto(viewer + "#" + category)
        frame = page.frame_locator("#content_iframe")
        toolbar = page.locator("#kiwixtoolbar")
        def assert_toolbar_visible():
            bounds = toolbar.bounding_box()
            assert bounds and bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= page.viewport_size["height"]
            expect(page.get_by_role("link", name="Back to library")).to_be_visible()

        def scroll_article():
            # First let the upstream poll observe the initial position.
            page.wait_for_function("document.querySelector('#kiwixtoolbar').style.position === 'static'")
            frame.locator("body").evaluate("(body) => body.ownerDocument.defaultView.scrollTo(0, 650)")
            # Confirm the simulated upstream requested an off-screen toolbar.
            page.wait_for_function("document.querySelector('#kiwixtoolbar').style.top === '-100%'")
            assert_toolbar_visible()
            page.set_viewport_size({"width": page.viewport_size["width"], "height": page.viewport_size["height"] - 80})
            assert_toolbar_visible()
            frame.locator("body").evaluate("(body) => body.ownerDocument.defaultView.scrollTo(0, 0)")
            page.wait_for_function("document.querySelector('#kiwixtoolbar').style.position === 'static'")
            assert_toolbar_visible()
            page.set_viewport_size({"width": page.viewport_size["width"], "height": page.viewport_size["height"] + 80})

        expect(frame.get_by_text("Bowline")).to_be_visible()
        if page.viewport_size["width"] < 520:
            scroll_article()
        frame.get_by_text("Bowline").click()
        expect(page).to_have_url(viewer + "#" + bowline)
        expect(frame.get_by_role("heading", name="Bowline")).to_be_visible()
        assert_toolbar_visible()
        if page.viewport_size["width"] < 520:
            scroll_article()
        page.evaluate("history.back()")
        expect(page).to_have_url(viewer + "#" + category)
        frame.get_by_role("link", name="Basic knots").focus()
        page.keyboard.press("Enter")
        expect(page).to_have_url(viewer + "#" + basic)
        expect(frame.get_by_role("heading", name="Basic knots")).to_be_visible()
        assert_toolbar_visible()
        # Direct deep links must still open inside the toolbar-bearing shell.
        page.goto("about:blank")
        page.goto(viewer + "#" + bowline)
        expect(frame.get_by_role("heading", name="Bowline")).to_be_visible()
        assert_toolbar_visible()
        if page.viewport_size["width"] < 520:
            scroll_article()
    finally:
        page.unroute("**/zim/**", serve)


def exercise(browser, base, name, viewport, artifacts):
    context = browser.new_context(viewport=viewport, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(
        f"{page.url}: {error}\\n{getattr(error, 'stack', '')}"))
    try:
        check_kiwix_article_navigation(page, base)
        # Test the application through real browser requests, not mock HTML.
        page.goto(base + "/")
        expect(page.get_by_role("link", name="Getting Started")).to_be_visible()
        search_input = page.locator("#library-search")
        expect(search_input).to_be_hidden()
        search_toggle = page.locator('summary[aria-label="Search library"]')
        search_toggle.focus()
        page.keyboard.press("Enter")
        expect(search_input).to_be_visible()
        search_input.fill("emergency")
        search_input.press("Enter")
        expect(page).to_have_url(base + "/search?q=emergency&mode=progressive")
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
                dict(source_id="timeout", name="Slow source", status="timeout", results=[]),
                dict(source_id="no-text", name="Image-only document", status="no_text", results=[]),
                dict(source_id="no-match", name="Unrelated source", status="ok", results=[]),
            ]

        with patch.object(admin_api.AdminAPIHandler, "_search_published_content", side_effect=search_results):
            page.goto(base + "/search?q=water")
            section = page.get_by_role("region", name="Matching source")
            expect(section.get_by_role("listitem")).to_have_count(20)
            expect(section.get_by_role("link", name="Water result 20")).to_be_visible()
            disclosure = section.locator("summary")
            disclosure.focus()
            page.keyboard.press("Enter")
            expect(section.get_by_role("link", name="Water result 20")).to_be_hidden()
            page.keyboard.press("Enter")
            expect(section.get_by_role("link", name="Water result 20")).to_be_visible()
            issue = page.get_by_text("Slow source — Search timed out")
            expect(issue).to_be_hidden()
            page.get_by_text("Search incomplete · 1 affected source").click()
            expect(issue).to_be_visible()
            expect(page.get_by_text("Image-only document")).to_have_count(0)
            expect(page.get_by_text("Unrelated source")).to_have_count(0)
            section.get_by_role("link", name="Next page").click()
            expect(section.get_by_role("listitem")).to_have_count(2)
            expect(section.get_by_role("link", name="Water result 21")).to_be_visible()
            expect(section.get_by_role("link", name="Next page")).to_have_count(0)
            section.get_by_role("link", name="Previous page").click()
            expect(section.get_by_role("link", name="Water result 20")).to_be_visible()

        progressive_calls = []

        def progressive_results(query, sources, *, page, start_base):
            source_id = sources[0]["source_id"]
            progressive_calls.append((source_id, page))
            failed = source_id == "slow" and progressive_calls.count(("slow", 1)) == 1
            start = (page - 1) * 20 + 1
            return [dict(source_id=source_id, status="timeout" if failed else "ok",
                         page=page, has_more=page == 1 and not failed, start_base=start_base,
                         results=[] if failed else [
                             admin_api.library_search.Result(f"{source_id} result {i}", f"/fixture/{i}")
                             for i in range(start, min(start + 20, 23))
                         ])]

        with patch.object(admin_api.AdminAPIHandler, "_public_library", return_value=(
                [dict(source_id="matching", name="Matching source"), dict(source_id="slow", name="Slow source")], []
        )), patch.object(admin_api.AdminAPIHandler, "_search_published_content", side_effect=progressive_results):
            page.goto(base + "/search?q=water&mode=progressive")
            section = page.locator('[data-source="matching"]')
            expect(section.get_by_role("listitem")).to_have_count(4)
            retry = page.get_by_role("button", name="Retry Slow source")
            expect(retry).to_be_visible()
            section.locator("summary").click()
            expect(section.get_by_role("link", name="matching result 1")).to_be_hidden()
            retry.click()
            expect(page.get_by_role("link", name="slow result 1")).to_be_visible()
            expect(section.get_by_role("link", name="matching result 1")).to_be_hidden()
            section.locator("summary").click()
            more = section.get_by_role("button", name="Show more from Matching source")
            for _ in range(5):
                more.click()
            expect(section.get_by_role("link", name="matching result 22")).to_be_visible()
            expect(more).to_be_hidden()
            expect(page).to_have_url(base + "/search?q=water&mode=progressive")
            assert sorted(progressive_calls) == [("matching", 1), ("matching", 2), ("slow", 1), ("slow", 1)]

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
        other_tab = context.new_page()
        try:
            other_tab.goto(base + "/")
            other_tab.bring_to_front()
            page.bring_to_front()
            page.reload()
            expect(page).to_have_url(base + "/admin")
            expect(page.locator("h1")).to_contain_text("Manage offline sources")
        finally:
            other_tab.close()
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
            for protected in ("/admin", "/admin/sources", "/admin/groupings", "/admin/settings", "/admin/updates"):
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
        page.get_by_label("More admin tools").click()
        guide_trigger.focus()
        expect(guide_trigger).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#source-guide")).to_be_hidden()
        # Opening the guide a second time must work after an Escape dismissal.
        page.get_by_label("More admin tools").click()
        guide_trigger.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#source-guide")).to_be_hidden()
        # Dismissed guidance must not trap subsequent keyboard navigation.
        page.get_by_label("More admin tools").click()
        guide_trigger.focus()
        expect(guide_trigger).to_be_focused()
        page.get_by_label("More admin tools").click()

        filter_toggle = page.locator("#source-filter-toggle")
        filter_toggle.focus()
        expect(filter_toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-filters-panel")).to_be_visible()
        filters = page.locator("#source-filters-panel")
        first_source = page.locator(".source-card").first
        assert filters.bounding_box()["y"] < first_source.bounding_box()["y"]
        expect(page.locator("#source-filter-count")).to_be_visible()
        expect(page.locator("#group-panel")).to_be_hidden()
        expect(page.locator("#source-filter-toggle")).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#source-filter-search")).to_be_hidden()
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
        assert page.locator("#catalog-update-policy option").evaluate_all("(options) => options.map(o => o.value)") == ["notify", "automatic", "scheduled", "disabled"]
        page.locator("#catalog-update-policy").select_option("notify")
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
        page.locator("#mobile-search-toggle").click()
        search.fill("not-a-match-" + name)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_hidden()
        # Case-insensitive search and whitespace handling are important for
        # source discovery on narrow/mobile screens.
        search.fill(title.upper())
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        search.fill(title)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        search.fill("")

        # The desktop toolbar stays compact, with selected counts and actions
        # hidden until the operator enters select mode.
        expect(page.get_by_role("navigation", name="Administration tools")).to_be_visible()
        expect(page.locator("#mobile-selection")).to_be_hidden()
        expect(page.locator(".source-order-controls").first).to_be_hidden()
        # Bulk-selection state and keyboard access: no action is executed.
        selection_toggle = page.locator("#source-select-mode")
        page.get_by_label("More admin tools").click()
        selection_toggle.focus()
        expect(selection_toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(selection_toggle).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#group-select-all")).to_be_visible()
        expect(page.locator("#group-count")).to_contain_text("0 of ")
        checks = page.locator("[data-group-select]")
        total = checks.count()
        for index in range(3):
            checks.nth(index).check()
        expect(page.locator("#group-count")).to_have_text(f"3 of {total} sources selected")
        expect(page.locator("#source-selection-panel")).to_be_hidden()
        page.evaluate("window.scrollTo(0, 650)")
        bar = page.locator("#mobile-selection")
        toolbar = page.get_by_role("navigation", name="Administration tools")
        assert abs(bar.bounding_box()["y"] - toolbar.bounding_box()["height"]) < 2
        page.locator("#mobile-selection-done").click()
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
        expect(page.locator("#catalog-update-policy")).to_have_value("notify")
        expect(page.locator("#catalog-update-periodic")).to_have_count(0)
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
        # reset and counts without a sort selector.
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
        expect(page.locator("#source-filter-sort")).to_have_count(0)
        page.locator("#source-filter-toggle").click()

        # Closed #71: selection checkboxes are opt-in and bulk publication
        # remains gated for the unverified disposable source.
        page.get_by_label("More admin tools").click()
        page.locator("#source-select-mode").click()
        check = page.locator('[data-group-select][value="' + sid + '"]')
        check.focus()
        expect(check).to_be_focused()
        page.keyboard.press("Space")
        expect(check).to_be_checked()
        expect(page.locator("#group-count")).to_have_text(re.compile(r"^1 of \d+ sources selected$"))
        page.locator("#mobile-actions-open").click()
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
        # An unrelated profile change should reconcile without reload.
        before = admin_api.SOURCE_CATALOG.get()
        unrelated = next(row for row in before["sources"] if row["source_id"] != sid)
        updated = admin_api.SOURCE_CATALOG.mutate("edit", unrelated["source_id"],
            {"description": "Updated by the fixture while the page is open"}, before["revision"])
        page.wait_for_function("(rev) => window.axiomSourceRevision() === rev", arg=updated["revision"])
        expect(page.get_by_role("button", name="Reload sources")).to_have_count(0)
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
        page.get_by_label("More admin tools").click()
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
        for path in ("/admin", "/admin/sources", "/admin/groupings", "/admin/settings", "/admin/updates"):
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
            if not logout.is_visible():
                page.get_by_label("More admin tools").click()
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


def exercise_automatic_eligibility(browser, viewport):
    """The rolled-back source shows its policy without any extra grant control."""
    from test_admin_page_template import profile, render

    fixture = profile("devdocs", format="zim", source_url="https://publisher.example/devdocs.zim")
    fixture.update(installed_verified=True, discovery={"method": "archive", "periodic": True},
                   replacement={"state": "rolled_back", "sha256": "a" * 64},
                   update_check={"state": "update_available", "evidence": "Fresh fixture evidence"})
    page = browser.new_page(viewport=viewport)
    try:
        page.route("http://fixture.local/admin/sources", lambda route: route.fulfill(
            content_type="text/html", body=render([fixture])))
        page.route("http://fixture.local/api/sources", lambda route: route.fulfill(
            json={"revision": 0, "sources": [fixture]}))
        for policy, upcoming in (("automatic", "Next queue poll if eligible"),
                                 ("automatic", "Paused for new downloads"),
                                 ("scheduled", "Next window: 2026-11-27 23:00 (server local)")):
            fixture["automatic_update"] = {"policy": policy, "next": upcoming, "attempt": "completed"}
            mock_idle_update_notifications(page)
            page.goto("http://fixture.local/admin/sources")
            card = page.locator('.source-card[data-source-id="devdocs"]')
            card.locator(":scope > summary").click()
            details = card.locator("details.source-technical")
            details.locator("summary").click()
            expect(details.get_by_text(f"Update policy: {policy.capitalize()} · {upcoming}", exact=False)).to_be_visible()
            expect(details).to_contain_text("Fresh fixture evidence")
            expect(card.locator("[data-automatic-rearm]")).to_have_count(0)
            expect(card.get_by_text("Allow automatic update again")).to_have_count(0)
            expect(card).not_to_contain_text("Automatic update suppressed")
        fixture["automatic_update"] = {"policy": "scheduled", "next": "Maintenance window open",
                                       "check": "Source check failed; retry pending",
                                       "server_time": "2026-11-27 23:00:01 EST", "time_zone": "America/New_York"}
        expect(details).to_contain_text("Maintenance window open", timeout=8000)
        expect(details).to_contain_text("Source check failed; retry pending")
        expect(details).to_contain_text("Server local: 2026-11-27 23:00:01 EST (America/New_York)")
        expect(details).not_to_contain_text("Next window:")
    finally:
        page.close()


def exercise_history_poll(browser, viewport):
    """Use the real source-card DOM and poller with two independent timelines."""
    from test_admin_page_template import profile, render

    first = dict(profile("history-first", format="zim", source_url="https://publisher.org/first.zim"),
                 publication={"enabled": True, "published": True}, publication_filter="published",
                 acquisition=None, installed_verified=True, artifact={"bytes": 2097152})
    second = dict(profile("history-second", format="zim", source_url="https://publisher.org/second.zim"),
                  publication={}, acquisition=None)
    page = browser.new_page(viewport=viewport)
    changed = False
    fail_poll = False

    def poll(route):
        if fail_poll:
            route.abort()
            return
        update = ({"state": "unable_to_determine", "last_known_candidate_edition": "2026-11", "stale": True}
                  if changed else {"state": "update_available", "installed_edition": "2026-10",
                                   "candidate_edition": "2026-11", "candidate_bytes": 1048576})
        sources = [dict(first, update_check=update, acquisition={
            "state": "failed" if changed else "downloading",
            "active": not changed, "downloaded_bytes": 50, "expected_bytes": 100,
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
        mock_idle_update_notifications(page)
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
        progress = card.locator('[data-field="summary-progress"]')
        feedback = page.locator("#source-refresh-feedback")
        expect(progress).to_have_attribute("value", "50")
        expect(feedback).to_be_hidden()
        fail_poll = True
        expect(feedback).to_be_visible()
        expect(progress).to_be_visible()
        expect(progress).to_have_attribute("value", "50")
        expect(other.locator('[data-field="status"]')).to_have_text("Paused")
        expect(card).to_have_attribute("data-filter-publication", "published")
        fail_poll = False
        changed = True
        expect(feedback).to_be_hidden()
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
            route.fulfill(json={"enabled": enabled, "revision": revision})
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
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        card = page.locator('[data-source-id="action-target"]')
        summary = card.locator(".source-header")
        download = card.locator("[data-pdf-download]")
        feedback = card.locator("[data-pdf-feedback]")
        publish = card.locator('[data-field="publication-toggle"]')
        search = page.locator("#source-filter-search")
        page.locator("#source-filter-toggle").click()
        page.locator("#source-filter-format").select_option("pdf")
        expect(card).to_be_visible()
        page.locator("#source-filter-format").select_option("")
        page.locator("#mobile-search-toggle").click()
        search.fill("Fixture")
        page.get_by_label("More admin tools").click()
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
        after = card.evaluate("(node) => node.getBoundingClientRect().top")
        # Downloaded sources move to the front. The compact header can put
        # the new first source above its previous viewport position even
        # when scroll restoration reaches the top of the document.
        assert abs(after - top) <= 2 or (
            page.evaluate("scrollY") == 0 and 0 <= after < top
        )

        page.keyboard.press("Enter")
        expect(feedback).to_have_text("Fixture download failed")
        expect(download).to_be_focused()
        page.keyboard.press("Enter")
        expect(card.locator('[data-field="status"]')).to_have_text("2.05 kB")
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()
        expect(search).to_have_value("Fixture")

        # Start below the fold in default order. Other downloaded cards keep
        # the target below the fold until publication moves it to the front.
        for source in fillers[:24]:
            source["download_filter"] = "downloaded"
        expect(page.locator(".source-card").nth(24)).to_have_attribute("data-source-id", "action-target")
        publish.scroll_into_view_if_needed()
        publish.focus()
        page.keyboard.press("Enter")
        expect(page.locator(".source-card").first).to_have_attribute("data-source-id", "action-target")
        expect(publish).to_have_text("Unpublish")
        expect(page.locator("#filtered-action-feedback")).to_have_text("Fixture 30 PDF — Publication enabled.")
        expect(page.locator("#filtered-action-feedback")).to_be_visible()
        expect(page.locator("#filtered-action-feedback")).to_be_hidden(timeout=8000)
        expect(publish).to_be_in_viewport()
        expect(publish).to_be_focused()
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()

        page.locator("#source-filter-publication").select_option("published")
        publish.click()
        expect(card).to_be_hidden()
        expect(page.locator("#filtered-action-feedback")).to_have_text("Fixture 30 PDF — Source unpublished.")
        expect(page.locator("#filtered-action-feedback")).to_be_in_viewport()
        expect(page.locator("#filtered-action-feedback")).to_be_hidden(timeout=8000)
        page.locator("#source-filter-publication").select_option("")
        page.locator("#source-filter-publication").select_option("unpublished")
        publish.click()
        expect(card).to_be_hidden()
        expect(page.locator("#filtered-action-feedback")).to_have_text("Fixture 30 PDF — Publication enabled.")
        expect(page.locator("#filtered-action-feedback")).to_be_hidden(timeout=8000)
        page.locator("#source-filter-publication").select_option("")
        expect(card).to_have_attribute("open", "")
        expect(selected).to_be_checked()
        assert len(navigations) == 1, navigations
        assert not errors, errors
    finally:
        page.close()



def exercise_source_revision_reconciliation(browser, viewport):
    """Use actual source-page JS with controlled independent document mutations."""
    from copy import deepcopy
    from test_admin_page_template import profile, render

    rows = []
    for sid, kind in (("fixture-pdf", "pdf"), ("fixture-epub", "epub")):
        row = profile(sid, title=sid, format=kind, source_url=f"https://publisher.org/test.{kind}",
                      sha256="a" * 64, expected_size_bytes="100", update_policy="notify")
        row.update(installed_verified=True, artifact={"bytes": 100, "sha256": "a" * 64},
                   publication={"enabled": True, "published": True, "entry_path": "/reader/" + sid},
                   publication_filter="published", download_filter="downloaded",
                   discovery={"method":"head","periodic":True}, automatic_update={"policy":"notify"},
                   replacement={"state":"verified","sha256":"b" * 64,"review_token":"c" * 64})
        rows.append(row)
    revision = 0
    requests = []
    conflict_once = True
    page = browser.new_page(viewport=viewport)
    navigations = []
    errors = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url))
    page.on("pageerror", lambda error: errors.append(str(error)))

    def snapshot():
        return dict(revision=revision, sources=deepcopy(rows), groups=[])

    def api(route):
        nonlocal revision, conflict_once
        if route.request.method == "GET":
            route.fulfill(json=snapshot())
            return
        p = route.request.post_data_json
        sid = p["source_id"]
        row = next((r for r in rows if r["source_id"] == sid), None)
        requests.append(deepcopy(p))
        if p["revision"] != revision:
            route.fulfill(status=409, json={"error":"catalog revision changed"})
            return
        if row is None:
            route.fulfill(status=409, json={"error":"source not found"})
            return
        if route.request.url.endswith("/api/sources"):
            if conflict_once:
                conflict_once = False
                # An unrelated change in the interval between GET and POST.
                revision += 1
                route.fulfill(status=409, json={"error":"catalog revision changed"})
                return
            row["configuration"].update(p["fields"])
            row["automatic_update"]["policy"] = row["configuration"]["update_policy"]
        else:
            action = p["action"]
            if action == "zim-replacement-delete-verified":
                assert row["replacement"]["state"] == "rolled_back"
                assert p["candidate_sha256"] == "b" * 64
                row["replacement"] = None
            else:
                assert p["expected_replacement"] == row["replacement"]["review_token"]
                if action == "zim-replacement-activate":
                    assert row["replacement"]["state"] in {"verified", "rolled_back"}
                    row["replacement"].update(state="activated", review_token="d" * 64)
                    row["artifact"] = {"bytes": 200, "sha256":"b" * 64}
                else:
                    assert action == "zim-replacement-rollback"
                    assert row["replacement"]["state"] == "activated"
                    row["replacement"].update(state="rolled_back", review_token="e" * 64)
                    row["artifact"] = {"bytes": 100, "sha256":"a" * 64}
                row["configuration"].update(sha256=row["artifact"]["sha256"],
                                            expected_size_bytes=str(row["artifact"]["bytes"]))
        revision += 1
        route.fulfill(json={"revision":revision,"accepted":True})

    try:
        markup = render(catalog=snapshot())
        page.route("http://fixture.local/admin/sources", lambda route: route.fulfill(
            content_type="text/html", body=markup))
        page.route("http://fixture.local/api/*", api)
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        page.on("dialog", lambda dialog: dialog.accept())
        first, second = [page.locator(f'[data-source-id="{row["source_id"]}"]') for row in rows]
        for card in (first, second):
            card.locator(".source-header").click()
            expect(card.locator('[data-zim-replacement="activate"]')).to_be_visible()
        # Trigger both clicks in the same browser task; do not wait for polling.
        page.evaluate("""() => document.querySelectorAll('[data-zim-replacement="activate"]').forEach(b => b.click())""")
        for card in (first, second):
            expect(card.locator('[data-zim-replacement="rollback"]')).to_be_visible()
            expect(card.locator('[data-field="status"]')).to_have_text("200 B")
        assert [r["revision"] for r in requests] == [0,1], requests
        page.evaluate("""() => document.querySelectorAll('[data-zim-replacement="rollback"]').forEach(b => b.click())""")
        for card in (first, second):
            card.get_by_text("More actions", exact=True).click()
            expect(card.locator("[data-delete-staged-update]")).to_be_visible()
            expect(card.locator('[data-field="status"]')).to_have_text("100 B")
            expect(card).to_have_attribute("open", "")
        assert [r["revision"] for r in requests] == [0,1,2,3], requests
        # Republish retained verified bytes after rollback, without re-download.
        for card in (first, second):
            expect(card.locator('[data-zim-replacement="activate"]')).to_be_visible()
        page.evaluate("""() => document.querySelectorAll('[data-zim-replacement="activate"]').forEach(b => b.click())""")
        for card in (first, second):
            expect(card.locator('[data-zim-replacement="rollback"]')).to_be_visible()
            expect(card.locator('[data-field="status"]')).to_have_text("200 B")
        page.evaluate("""() => document.querySelectorAll('[data-zim-replacement="rollback"]').forEach(b => b.click())""")
        for card in (first, second):
            expect(card.locator("[data-delete-staged-update]")).to_be_visible()
            expect(card.locator('[data-field="status"]')).to_have_text("100 B")
        assert [r["revision"] for r in requests] == list(range(8)), requests
        second.locator("[data-delete-staged-update]").click()
        expect(second.locator("[data-delete-staged-update]")).to_be_hidden()
        # Keep an open editor through an unrelated update.
        first.locator("[data-catalog-edit]").click()
        page.locator("#catalog-update-policy").select_option("disabled")
        rows[1]["configuration"]["update_policy"] = "disabled"
        rows[1]["automatic_update"]["policy"] = "disabled"
        revision += 1
        page.wait_for_function("(rev) => window.axiomSourceRevision() === rev", arg=revision)
        expect(page.locator("#catalog-update-policy")).to_have_value("disabled")
        page.locator('#source-form [type="submit"]').click()
        expect(page.locator("#source-dialog")).to_be_hidden()
        expect(first.locator('[data-field="discovery-status"]')).to_have_text("Update policy: Disabled")
        edits = [r for r in requests if r.get("operation") == "edit"]
        assert len(edits) == 2 and all(r["fields"] == {"update_policy":"disabled"} for r in edits), edits

        # A conflicting edit to the same field is not overwritten.
        first.locator("[data-catalog-edit]").click()
        page.locator("#catalog-update-policy").select_option("notify")
        rows[0]["configuration"]["update_policy"] = "automatic"
        rows[0]["automatic_update"]["policy"] = "automatic"
        revision += 1
        page.wait_for_function("(rev) => window.axiomSourceRevision() === rev", arg=revision)
        page.locator('#source-form [type="submit"]').click()
        expect(page.locator("#source-form-feedback")).to_contain_text("Source changed in: update_policy")
        expect(page.locator("#catalog-update-policy")).to_have_value("notify")
        assert rows[0]["configuration"]["update_policy"] == "automatic"
        page.locator("#source-cancel").click()
        first.locator("[data-catalog-edit]").click()
        page.locator("#catalog-update-policy").select_option("notify")
        rows.pop(0)
        revision += 1
        before = len(requests)
        page.locator('#source-form [type="submit"]').click()
        expect(page.locator("#source-form-feedback")).to_contain_text("Source was removed")
        expect(page.locator("#catalog-update-policy")).to_have_value("notify")
        assert len(requests) == before
        page.locator("#source-cancel").click()
        assert len(navigations) == 1, navigations
        assert not errors, errors
    finally:
        page.close()


def exercise_source_order(browser, viewport):
    from test_admin_page_template import profile, render

    sources = [
        dict(profile(sid, title=title), publication_filter=status)
        for sid, title, status in [
            ("first", "Zulu", "published"),
            ("second", "Alpha", "published"),
            ("third", "Archive", "unpublished"),
        ]
    ]
    order = ["first", "second"]
    revision = 0

    def admin(route):
        catalog = dict(sources=sources, revision=revision, library_order=order)
        route.fulfill(content_type="text/html", body=render(catalog=catalog))

    def api(route):
        nonlocal order, revision
        if route.request.method == "GET":
            route.fulfill(json=dict(revision=revision, sources=sources, library_order=order))
            return
        payload = route.request.post_data_json
        assert payload["revision"] == revision
        order = payload["layout"]["source_order"]
        revision += 1
        route.fulfill(json=dict(revision=revision, layout=payload["layout"]))

    page = browser.new_page(viewport=viewport)
    try:
        page.route("http://fixture.local/admin/sources", admin)
        page.route("http://fixture.local/api/*", api)
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        cards = page.locator(".source-card")
        expect(cards.first).to_have_attribute("data-source-id", "first")
        expect(page.locator(".publication-divider .source-card")).to_have_attribute("data-source-id", "third")
        def open_reorder_tools():
            expect(page.locator(".source-order-controls").first).to_be_hidden()
            page.get_by_label("More admin tools").click()
            page.get_by_role("button", name="Reorder published sources").click()

        open_reorder_tools()
        cards.first.locator("summary.source-header").click()
        button = page.get_by_role("button", name="Move Zulu down")
        button.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#source-order-feedback")).to_have_text("Source order saved.")
        expect(cards.first).to_have_attribute("data-source-id", "second")
        expect(cards.nth(1)).to_have_attribute("open", "")
        expect(page.get_by_role("button", name="Move Zulu up")).to_be_focused()
        page.reload()
        expect(cards.first).to_have_attribute("data-source-id", "second")
        open_reorder_tools()
        page.locator("#mobile-search-toggle").click()
        page.locator("#source-filter-search").fill("Zulu")
        expect(page.get_by_role("button", name="Move Zulu up")).to_be_disabled()
        page.locator("#source-filter-search").clear()
        expect(cards.first).to_have_attribute("data-source-id", "second")
        grips = page.locator(".source-order-controls:not([hidden]) [data-order-grip]")
        grips.nth(1).scroll_into_view_if_needed()
        origin = grips.first.bounding_box()
        destination = grips.nth(1).bounding_box()
        assert origin and destination
        page.mouse.move(origin["x"] + origin["width"] / 2, origin["y"] + origin["height"] / 2)
        page.mouse.down()
        page.mouse.move(destination["x"] + destination["width"] / 2,
                        destination["y"] + destination["height"] / 2, steps=5)
        page.mouse.up()
        expect(cards.first).to_have_attribute("data-source-id", "first")
    finally:
        page.close()


def exercise_compact_sources(browser, viewport):
    """Exercise the mobile toolbar and sheets with enough rows to scroll."""
    from test_admin_page_template import profile, render

    sources = [dict(profile(f"source-{i:02}", title=f"Source {i:02}"),
                    publication_filter="published" if i < 20 else "unpublished",
                    download_filter="not-downloaded") for i in range(40)]
    page = browser.new_page(viewport=viewport, has_touch=True, is_mobile=True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.route("**/admin/sources", lambda route: route.fulfill(
            content_type="text/html", body=render(sources)))
        page.route("**/api/sources", lambda route: route.fulfill(json={"sources": sources, "revision": 0}))
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        toolbar = page.get_by_role("navigation", name="Administration tools")
        cards = page.locator(".source-card")
        menu = toolbar.get_by_label("More admin tools")
        expect(toolbar).to_be_visible()
        assert cards.first.bounding_box()["y"] < 160
        assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")

        toolbar.get_by_text("Sources ▾").click()
        expect(toolbar.get_by_role("link", name="Groupings")).to_be_visible()
        expect(toolbar.get_by_role("link", name="Settings")).to_be_visible()
        expect(toolbar.get_by_role("link", name="Back to library")).to_be_visible()
        toolbar.get_by_text("Sources ▾").click()
        toolbar.get_by_role("button", name="Add Source").click()
        expect(page.locator("#source-dialog")).to_be_visible()
        page.keyboard.press("Escape")
        menu.click()
        page.get_by_role("button", name="Content source guide").click()
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")

        page.evaluate("window.scrollTo(0, 700)")
        assert toolbar.bounding_box()["y"] == 0
        start_y = page.evaluate("window.scrollY")
        page.get_by_role("button", name="Filter sources").click()
        sheet = page.get_by_role("dialog", name="Filters")
        sheet.get_by_label("Publication").select_option("published")
        expect(sheet.get_by_role("button", name="Show 20 sources")).to_be_visible()
        assert cards.filter(visible=True).count() == 40
        sheet.get_by_role("button", name="Close").click()
        expect(sheet).to_be_hidden()
        assert page.evaluate("window.scrollY") == start_y
        page.get_by_role("button", name="Filter sources").click()
        expect(sheet.get_by_label("Publication")).to_have_value("")
        sheet.get_by_label("Publication").select_option("published")
        sheet.get_by_role("button", name="Show 20 sources").click()
        expect(page.locator("#source-filter-count")).to_have_text("20 of 40 sources")

        toolbar.get_by_role("button", name="Search sources").click()
        search = page.get_by_role("searchbox")
        search.fill("Source 10")
        expect(page.locator("#source-filter-count")).to_have_text("1 of 40 sources")
        menu.click()
        page.get_by_role("button", name="Select", exact=True).click()
        bar = page.locator("#mobile-selection")
        expect(bar.get_by_text("Select all 1")).to_be_visible()
        bar.get_by_role("checkbox").check()
        expect(bar.get_by_text("1 of 40 sources selected")).to_be_visible()
        bar.get_by_role("button", name="Actions").click()
        actions = page.get_by_role("dialog", name="Actions")
        expect(actions.get_by_role("button", name="Publish", exact=True)).to_be_disabled()
        actions.get_by_text("Organize sources", exact=False).click()
        expect(actions.get_by_label("Grouping type")).to_be_visible()
        actions.get_by_role("button", name="Close").click()
        bar.get_by_role("button", name="Done").click()
        expect(bar).to_be_hidden()
        assert not errors, errors
    finally:
        page.close()



def exercise_admin_navigation(browser, viewport):
    from test_admin_page_template import render

    mobile = viewport["width"] <= 640
    page = browser.new_page(viewport=viewport, has_touch=mobile, is_mobile=mobile)
    try:
        def admin(route):
            section = route.request.url.rsplit("/", 1)[-1]
            route.fulfill(content_type="text/html", body=render(
                admin_page=section, session_csrf_token="fixture"))
        page.route("http://fixture.local/admin/*", admin)
        page.route("http://fixture.local/api/**", lambda route: route.fulfill(json={}))
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        toolbar = page.get_by_role("navigation", name="Administration tools")
        for current, next_page in (("Sources", "Updates"), ("Updates", "Settings"),
                                   ("Settings", "Groupings"), ("Groupings", "Sources")):
            expect(toolbar).to_be_visible()
            expect(page.locator(".site-header")).to_be_hidden()
            expect(page.locator(".admin-nav")).to_be_hidden()
            brand = toolbar.get_by_role("link", name="The Axiom", exact=True)
            if mobile:
                expect(brand).to_be_hidden()
            else:
                expect(brand).to_be_visible()
                expect(brand).to_have_attribute("href", "/")
            if current != "Sources":
                expect(toolbar.get_by_role("button", name="Filter sources")).to_have_count(0)
            toolbar.get_by_label("More admin tools").click()
            expect(toolbar.get_by_role("button", name="Sign out")).to_be_visible()
            toolbar.get_by_text(current + " ▾").click()
            expect(toolbar.get_by_role("link", name=current)).to_have_attribute("aria-current", "page")
            expect(toolbar.get_by_role("link", name="Back to library")).to_have_attribute("href", "/")
            toolbar.get_by_role("link", name=next_page).click()
            expect(page).to_have_url("http://fixture.local/admin/" + next_page.lower())
            assert toolbar.bounding_box()["y"] == 0
        expect(toolbar.get_by_role("button", name="Filter sources")).to_be_visible()
    finally:
        page.close()



def mock_idle_update_notifications(page):
    """Serve the real shared script in mocked UI journeys, with an empty queue."""
    script = (Path(__file__).parents[1] / "templates/update-notifications.js").read_text()
    page.route("http://fixture.local/api/update-notifications.js", lambda route: route.fulfill(
        content_type="text/javascript", body=script))
    page.route("http://fixture.local/api/admin/update-notifications", lambda route: route.fulfill(
        json={"notification": None}))


def exercise_update_notifications(browser, viewport):
    import time
    script = (Path(__file__).parents[1] / "templates/update-notifications.js").read_text()
    page = browser.new_page(viewport=viewport)
    pending = []
    claimed = []
    acknowledged = []
    def api(route):
        payload = route.request.post_data_json
        if payload["action"] == "claim":
            item = next((item for item in pending if item not in claimed), None)
            if item:
                claimed.append(item)
            route.fulfill(json={"notification": item})
        else:
            acknowledged.append(payload["event_id"])
            route.fulfill(json={"acknowledged": True})
    try:
        page.route("http://fixture.local/", lambda route: route.fulfill(
            content_type="text/html", body='<button id="target">Still interactive</button><script src="/api/update-notifications.js"></script>'))
        page.route("http://fixture.local/api/update-notifications.js", lambda route: route.fulfill(
            content_type="text/javascript", body=script))
        page.route("http://fixture.local/api/update-notifications", api)
        page.goto("http://fixture.local/")
        toast = page.locator("#axiom-update-toast")
        expect(toast).to_be_hidden()
        pending.extend([dict(event_id="first", claim="lease-1", name="First source", edition="2026-11"),
                        dict(event_id="second", claim="lease-2", name="Second source", edition=None)])
        expect(toast).to_contain_text("First source updated to 2026-11.", timeout=12000)
        t = time.monotonic()
        page.locator("#target").click()
        page.wait_for_timeout(4000)
        expect(toast).to_contain_text("First source updated")
        assert not acknowledged
        expect(toast).to_contain_text("Second source updated.", timeout=12000)
        assert time.monotonic() - t >= 4.5
        assert acknowledged == ["first"]
        page.get_by_role("button", name="Dismiss update notification").click()
        expect(toast).to_be_hidden()
        deadline = time.monotonic() + 5
        while len(acknowledged) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert acknowledged == ["first", "second"]
        page.reload()
        expect(toast).to_be_hidden()
        assert len(claimed) == 2
    finally:
        page.close()


def exercise_source_activity(browser, viewport):
    from test_admin_page_template import render

    page = browser.new_page(viewport=viewport)
    events = [
        dict(sequence=2, timestamp="2026-11-27T05:00:00+00:00", name="Removed fixture",
             source_id="removed", action="removed", trigger="manual", outcome="completed"),
        dict(sequence=1, timestamp="2026-11-26T05:00:00+00:00", name="Other fixture",
             source_id="other", action="updated", trigger="scheduled", outcome="completed",
             old_edition="1", new_edition="2"),
    ]

    def api(route):
        from urllib.parse import urlsplit, parse_qs
        filters = parse_qs(urlsplit(route.request.url).query)
        rows = [row for row in events if
                ("source" not in filters or row["source_id"] == filters["source"][0]) and
                ("action" not in filters or row["action"] == filters["action"][0]) and
                ("before" not in filters or row["sequence"] < int(filters["before"][0]))]
        route.fulfill(json={"events": rows[:1], "next": rows[0]["sequence"] if len(rows) > 1 else None,
                            "sources": [{"source_id": row["source_id"], "name": row["name"]} for row in events],
                            "actions": ["removed", "updated"]})

    try:
        page.route("http://fixture.local/admin/activity", lambda route: route.fulfill(
            content_type="text/html", body=render(admin_page="activity", session_csrf_token="fixture")))
        page.route("http://fixture.local/api/source-activity*", api)
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/activity")
        expect(page.get_by_text("Removed fixture — Source removed")).to_be_visible()
        page.get_by_role("button", name="Older").click()
        expect(page.get_by_text("Other fixture — Update activated")).to_be_visible()
        expect(page.get_by_text("1 → 2", exact=False)).to_be_visible()
        page.get_by_label("Source", exact=True).select_option("removed")
        expect(page.get_by_text("Removed fixture — Source removed")).to_be_visible()
        page.get_by_label("Action", exact=True).select_option("updated")
        expect(page.get_by_text("No source activity recorded.")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2")
    finally:
        page.close()



def exercise_batch_update_journey(browser, viewport):
    from test_admin_page_template import profile, render

    page = browser.new_page(viewport=viewport)
    digest = "a" * 64
    sources = [profile("first", "First archive", format="zim"),
               profile("second", "Second archive", format="zim")]
    state = "idle"
    jobs = []
    calls = []
    checks = []
    pause_calls = []
    paused = False
    for source in sources:
        source.update(installed_verified=True, installed_edition="2026-10",
                      artifact={"bytes": 1024}, replacement=None,
                      update_check={"state": "up_to_date", "checked_at": 1795000000},
                      discovery={"method": "kiwix", "periodic": True},
                      automatic_update={"policy": "scheduled", "next": "Next window: tomorrow"})

    def candidate():
        return {"state": "update_available", "candidate_sha256": digest,
                "candidate_edition": "2026-11", "candidate_bytes": 2048,
                "checked_at": 1795000000}

    def api(route):
        nonlocal state, paused
        path = route.request.url.rsplit("/", 1)[-1]
        if path == "update-sweep":
            if route.request.method == "POST":
                assert route.request.post_data_json == {"confirm": True}
                checks.append("all")
                state = "completed"
                for source in sources:
                    source["update_check"] = candidate()
                route.fulfill(status=202, json={"state": "running"})
            else:
                route.fulfill(json={"state": state, "checks": (
                    [{"source_id": "inconclusive-fixture", "state": "unable_to_determine",
                      "evidence": "No strong ETag was saved for the installed file and this URL."}]
                    if state == "completed" else []
                )})
        elif path == "sources":
            count = sum(row["update_check"].get("state") == "update_available"
                        and not row["replacement"] for row in sources)
            staged = sum(bool(row["replacement"] and row["replacement"]["state"] == "verified")
                         for row in sources)
            route.fulfill(json={"revision": 1, "sources": sources, "available_updates": count + staged,
                                "update_stages": {"download": count, "publish": staged}})
        elif path == "update-batch":
            data = route.request.post_data_json
            calls.append(data)
            operation = data["operation"]
            jobs.insert(0, {"source_id": "first", "operation": operation, "state": "completed"})
            sources[0]["replacement"] = {"state": "verified" if operation == "download-update" else "activated",
                                         "sha256": digest, "expected_bytes": 2048}
            sources[0]["configuration"]["edition"] = "2026-11"
            sources[1]["update_check"] = {"state": "up_to_date"}
            if operation == "publish-update":
                sources[0]["update_check"] = {"state": "up_to_date"}
                sources[0]["installed_edition"] = "2026-11"
            route.fulfill(status=202, json={"queued": {"first": "fixture-job"}, "rejected": {}})
        elif path == "update-check":
            data = route.request.post_data_json
            checks.append(data)
            sources[1]["update_check"] = candidate()
            route.fulfill(json=sources[1]["update_check"])
        elif path == "acquisition-batch":
            route.fulfill(json={"jobs": jobs})
        elif path == "update-schedule":
            route.fulfill(json={"next_run": "2026-11-28T03:00:00-05:00", "time_zone": "America/New_York"})
        elif path == "automatic-update-pause":
            if route.request.method == "POST":
                paused = route.request.post_data_json["paused"]
                pause_calls.append(paused)
            route.fulfill(json={"paused": paused})
        else:
            route.fulfill(json={})

    def admin(route):
        section = route.request.url.rsplit("/", 1)[-1]
        route.fulfill(content_type="text/html", body=render(
            admin_page=section, catalog={"sources": sources, "revision": 1}, session_csrf_token="fixture"))

    try:
        page.route("http://fixture.local/admin/*", admin)
        page.route("http://fixture.local/api/**", api)
        page.on("dialog", lambda dialog: dialog.accept())
        mock_idle_update_notifications(page)
        page.goto("http://fixture.local/admin/sources")
        toolbar = page.get_by_role("navigation", name="Administration tools")
        expect(page.locator("#review-updates-link")).to_have_count(0)
        expect(toolbar.locator("[data-update-nav-count]").first).to_be_hidden()
        toolbar.get_by_text("Sources ▾").click()
        toolbar.get_by_role("link", name="Updates").click()
        expect(page).to_have_url("http://fixture.local/admin/updates")
        review = page.locator("#update-review")
        check_all = page.locator("#check-all-updates")
        download = page.locator("#download-updates")
        publish = page.locator("#publish-updates")
        filters = page.locator("#update-filter-state")
        expect(review).to_be_visible()
        expect(page.locator("#source-cards")).to_have_count(0)
        expect(review.get_by_text("No updates need attention.", exact=False)).to_be_visible()
        expect(download).to_be_disabled()
        expect(publish).to_be_disabled()
        assert not calls and not checks and not pause_calls
        expect(page.locator("#updates-schedule")).to_contain_text("2026-11-28")
        expect(review.get_by_role("link", name="Update schedule settings")).to_have_attribute("href", "/admin/settings#update-schedule-title")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2")
        filters.select_option("all")
        expect(review.locator('[data-update-state="current"] .badge')).to_have_text(["Up to date", "Up to date"])
        expect(review.get_by_text("Update policy: Scheduled", exact=False)).to_have_count(2)
        expect(review.get_by_text("Next window: tomorrow", exact=False)).to_have_count(2)
        filters.select_option("attention")
        check_all.click()
        expect(download).to_have_text("Download 2 updates")
        expect(review.get_by_text("Could not determine update status for 1 source.", exact=False)).to_be_visible()
        review.get_by_text("Inconclusive publisher checks").click()
        expect(review.get_by_text("No strong ETag was saved", exact=False)).to_be_visible()
        expect(review.get_by_text("Installed 2026-10 · Update 2026-11", exact=False)).to_have_count(2)
        expect(page.locator("#update-selection-size")).to_contain_text("4 KiB")
        # Filter selection is limited to visible rows and restored on clearing.
        page.locator("#update-filter-search").fill("First")
        expect(download).to_have_text("Download 1 update")
        page.locator("#update-filter-search").fill("")
        expect(download).to_have_text("Download 2 updates")
        filters.select_option("staged")
        expect(review.get_by_text("No sources match the selected update filters.")).to_be_visible()
        expect(download).to_be_disabled()
        filters.select_option("attention")
        expect(download).to_have_text("Download 2 updates")
        review.get_by_role("checkbox", name="Second archive").uncheck()
        expect(download).to_have_text("Download 1 update")
        download.click()
        expect(publish).to_have_text("Publish 1 update")
        expect(review.locator('[data-update-state="staged"] .badge')).to_have_text("Verified / staged")
        expect(review.get_by_text("Download completed")).to_be_visible()
        publish.click()
        filters.select_option("applied")
        expect(review.locator('[data-update-state="applied"] .badge')).to_have_text("Applied")
        expect(review.get_by_text("Publication completed")).to_be_visible()
        # Live refresh: automatic job appears without navigating or submitting.
        filters.select_option("attention")
        jobs.insert(0, {"source_id": "second", "operation": "automatic-update", "state": "queued"})
        expect(review.locator('[data-update-state="active"] .badge')).to_have_text("Queued", timeout=12000)
        jobs[0]["state"] = "failed"
        jobs[0]["detail"] = "fixture failure"
        expect(review.get_by_text("Automatic update failed · fixture failure")).to_be_visible(timeout=12000)
        filters.select_option("failed")
        expect(review.get_by_text("Failed / needs attention")).to_be_visible()
        # Global pause shares the settings API; individual checks share source API.
        page.locator("#updates-pause").click()
        expect(page.locator("#updates-pause")).to_have_text("Resume automatic updates")
        review.get_by_role("button", name="Check for updates for Second archive").click()
        filters.select_option("available")
        expect(review.locator('[data-update-state="available"] .badge')).to_have_text("Update available")
        assert checks == ["all", {"source_id": "second", "revision": 1}]
        assert pause_calls == [True]
        assert calls == [
            {"operation": "download-update", "revision": 1, "selection": {"first": digest}},
            {"operation": "publish-update", "revision": 1, "selection": {"first": digest}},
        ]
        review.get_by_role("link", name="Source details for Second archive").click()
        expect(page).to_have_url("http://fixture.local/admin/sources#source-second")
        expect(page.locator("#source-second")).to_have_attribute("open", "")
        toolbar = page.get_by_role("navigation", name="Administration tools")
        expect(toolbar.locator("[data-update-nav-count]").first).to_have_text("1")
        expect(toolbar.locator("[data-update-nav-count]").first).to_be_visible()
        toolbar.get_by_text("Sources ▾", exact=False).click()
        expect(toolbar.get_by_role("link", name="Updates")).to_have_attribute("href", "/admin/updates")
        toolbar.get_by_role("link", name="Updates").click()
        expect(page).to_have_url("http://fixture.local/admin/updates")
        assert len(calls) == 2
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
                                    if name == "desktop":
                                        timed_case("desktop-journey", name, exercise, browser,
                                                   f"http://{host}:{port}", name, viewport, artifacts)
                                        timed_case("source-actions", name, exercise_source_actions, browser, viewport)
                                        timed_case("update-notifications", name, exercise_update_notifications, browser, viewport)
                                    else:
                                        mobile_page = browser.new_page(viewport=viewport, is_mobile=True, has_touch=True)
                                        try:
                                            timed_case("kiwix-navigation", name, check_kiwix_article_navigation,
                                                       mobile_page, f"http://{host}:{port}")
                                        finally:
                                            mobile_page.close()
                                        timed_case("compact-sources", name, exercise_compact_sources, browser, viewport)
                                    for scenario, run in (
                                        ("admin-navigation", exercise_admin_navigation),
                                        ("source-activity", exercise_source_activity),
                                        ("batch-update", exercise_batch_update_journey),
                                        ("history-poll", exercise_history_poll),
                                        ("automatic-eligibility", exercise_automatic_eligibility),
                                        ("source-order", exercise_source_order),
                                        ("source-reconciliation", exercise_source_revision_reconciliation),
                                    ):
                                        timed_case(scenario, name, run, browser, viewport)
                            finally:
                                browser.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        worker.join(timeout=5)


if __name__ == "__main__":
    main()
