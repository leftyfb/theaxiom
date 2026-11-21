"""Chromium browser regression against the real admin HTTP handler.

Runs on isolated loopback with temporary catalog, credentials and configuration.
Never connects to Vancouver or opens a production archive.
"""
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
    context = browser.new_context(viewport=viewport, accept_downloads=False)
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
        # Dismissed guidance must not trap subsequent keyboard navigation.
        guide_trigger.focus()
        expect(guide_trigger).to_be_focused()

        filter_toggle = page.locator("#source-filter-toggle")
        filter_toggle.focus()
        expect(filter_toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#source-filters-panel")).to_be_visible()
        expect(page.locator("#source-filter-toggle")).to_have_attribute("aria-expanded", "true")
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
        page.locator("#catalog-format").select_option("unspecified")
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
        page.locator("#catalog-description").fill("Verified through real browser UI")
        # The application intentionally reloads after a catalog mutation.
        # Wait for that navigation, not just an already-present DOM element.
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('#source-form [type="submit"]').click()
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible(timeout=15000)

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
        page.locator("#source-filter-format").select_option("unspecified")
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
        check.focus()
        page.keyboard.press("Space")
        expect(check).not_to_be_checked()
        expect(page.locator("#bulk-selection")).to_be_hidden()
        page.locator("#source-select-mode").click()

        # Closed #3/#76: explicit cancel leaves source intact, confirmed
        # removal deletes ONLY the test catalog entry (no artifact exists).
        card = page.locator(".source-card[data-source-id='" + sid + "']")
        # Edit/save preserves expanded cards across reloads. Do not collapse
        # an already-open card before interacting with its Remove button.
        if not card.evaluate("(element) => element.open"):
            card.locator("summary.source-header").click()
        expect(card.locator("[data-catalog-remove]")).to_be_visible()
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
        assert not errors, f"{name} uncaught browser JavaScript errors: {errors}"
        print(f"PASS {name}: login, navigation, guide, filters, add/edit, selection, overflow, logout")
    except Exception:
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f"{name}-failure.png"), full_page=True)
        (artifacts / f"{name}-pageerrors.txt").write_text("\n".join(errors) or "(none)")
        raise
    finally:
        context.close()


def main():
    artifacts = Path(os.environ.get("AXIOM_BROWSER_ARTIFACTS", "/tmp/axiom-browser-artifacts"))
    with tempfile.TemporaryDirectory(prefix="axiom-browser-fixtures-") as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config"),
                                     "THEAXIOM_STORAGE_SETTINGS": str(root / "storage.json")}):
            with patch.object(admin_api, "SOURCE_CATALOG",
                              catalog_store.CatalogStore(root / "sources.json")):
                with patch.object(admin_api, "ADMIN_SESSIONS", admin_sessions.Sessions()):
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
                            browser = playwright.chromium.launch(headless=True)
                            try:
                                for name, viewport in (
                                    ("desktop", {"width": 1366, "height": 900}),
                                    ("mobile", {"width": 390, "height": 844}),
                                    ("narrow", {"width": 320, "height": 640}),
                                ):
                                    exercise(browser, f"http://{host}:{port}", name, viewport, artifacts)
                            finally:
                                browser.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        worker.join(timeout=5)


if __name__ == "__main__":
    main()
