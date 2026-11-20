import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


class AdminWebUISkeletonTests(unittest.TestCase):
    def test_accessible_shell_and_read_only_cards(self):
        text = TEMPLATE.read_text()
        for marker in ("<!doctype html>", 'lang="en"', 'meta name="viewport"',
                       "Skip to administration", 'id="main-content"', "Manage offline sources",
                       'data-source-id="{{ source.source_id }}"', '<details class="source-card"', '<summary class="source-header">', '<details class="source-technical">', '<summary>Technical details</summary>',
                       "No managed sources configured.", 'name="format"', "fetch('/api/sources'",
                       'id="status-staging"', 'id="status-library"'):
            self.assertIn(marker, text)
        for marker in ("data-pdf-upload", "data-pdf-download", "data-pdf-file",
                       "action:card.dataset.filterFormat === 'epub' ? 'epub-download' : 'pdf-download'", "acquisitionFetch('/api/pdf-upload'", "sessionStorage.setItem(stateKey"):
            self.assertTrue(marker in text, f"Missing PDF action marker: {marker}")
        for marker in ("data-dest=", "auth_basic", "proxy_pass",
                       "nginx", "systemd"):
            self.assertNotIn(marker, text.lower())
