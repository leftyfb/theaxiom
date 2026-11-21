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
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        # Test the application through real browser requests, not mock HTML.
        page.goto(base + "/")
        expect(page.locator("body")).to_contain_text("offline")
        page.goto(base + "/search?q=emergency")
        expect(page.locator("body")).to_be_visible()

        # Browser navigation must not bypass the sign-in boundary.
        page.goto(base + "/admin/sources")
        expect(page).to_have_url(base + "/admin/login")
        page.locator("#username").fill("admin")
        page.locator("#password").fill("wrong-password")
        page.locator("#submit").click()
        expect(page.locator("#feedback")).to_contain_text("Invalid")
        page.locator("#password").fill("browser-only-fixture-password")
        page.locator("#submit").click()
        expect(page).to_have_url(base + "/admin")
        expect(page.locator("h1")).to_contain_text("Manage offline sources")

        # A real modal and its controls need to be operable at both widths.
        page.locator("#source-guide-open").click()
        expect(page.locator("#source-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#source-guide")).to_be_hidden()

        page.locator("#source-filter-toggle").click()
        expect(page.locator("#source-filters-panel")).to_be_visible()
        expect(page.locator("#source-filter-toggle")).to_have_attribute("aria-expanded", "true")
        page.locator("#source-filter-toggle").click()
        expect(page.locator("#source-filters-panel")).to_be_hidden()

        # Exercise live JS form submission, dynamic reload, search and edit.
        page.locator("#source-add").click()
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

        page.locator("#source-filter-search").fill("not-a-match-" + name)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_hidden()
        page.locator("#source-filter-search").fill(title)
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible()
        page.locator("#source-filter-search").fill("")

        # Bulk-selection state and keyboard access: no action is executed.
        page.locator("#source-select-mode").click()
        expect(page.locator("#source-select-mode")).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#group-select-all")).to_be_visible()
        page.locator("#source-select-mode").click()
        expect(page.locator("#source-select-mode")).to_have_attribute("aria-pressed", "false")

        card = page.locator(".source-card[data-source-id='" + sid + "']")
        card.locator("summary.source-header").click()
        card.locator("[data-catalog-edit]").click()
        expect(page.locator("#source-dialog")).to_be_visible()
        expect(page.locator("#catalog-title")).to_have_value(title)
        page.locator("#catalog-description").fill("Verified through real browser UI")
        # The application intentionally reloads after a catalog mutation.
        # Wait for that navigation, not just an already-present DOM element.
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('#source-form [type="submit"]').click()
        expect(page.locator(".source-card[data-source-id='" + sid + "']")).to_be_visible(timeout=15000)

        # Mobile usability guard: horizontal viewport overflow breaks controls.
        page.goto(base + "/admin/groupings")
        expect(page.locator("h1")).to_be_visible()
        page.goto(base + "/admin/settings")
        expect(page.locator("h1")).to_be_visible()
        for path in ("/admin", "/admin/groupings", "/admin/settings"):
            page.goto(base + path)
            overflow = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth
            })""")
            assert overflow["scroll"] <= overflow["viewport"] + 2, (
                f"{name} {path} horizontal overflow: {overflow}")

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
