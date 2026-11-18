import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


class AdminWebUISkeletonTests(unittest.TestCase):
    def test_accessible_shell_and_read_only_cards(self):
        text = TEMPLATE.read_text()
        for marker in ("<!doctype html>", 'lang="en"', 'meta name="viewport"',
                       "Skip to administration", 'id="main-content"', "Manage offline sources",
                       'data-source-id="wikipedia-en"', "<details>", "<summary>Source details</summary>",
                       "No managed sources configured.", "offline_library_content_manifest",
                       "offline_library_kiwix_zim_enabled", "fetch('/api/acquisitions'",
                       'id="status-staging"', 'id="status-library"'):
            self.assertIn(marker, text)
        for marker in ("data-dest=", "auth_basic", "proxy_pass", "fetch('/api/action'",
                       "nginx", "systemd"):
            self.assertNotIn(marker, text.lower())
