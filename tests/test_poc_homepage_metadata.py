import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates" / "kiwix-homepage.html.j2"
CATALOG = ROOT / "config" / "catalog.yaml"


class POCHomepageMetadataTests(unittest.TestCase):
    def test_homepage_preserves_provenance_metadata_fields(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for field in (
            "publisher",
            "source_url",
            "retrieved",
            "original_filename",
            "bytes",
            "sha256",
            "redistribution_status",
        ):
            self.assertIn(f"document.{field}", text)
        for marker in (
            "provenance",
            "Source provenance",
            "provenance-label",
            "provenance-value",
            "word-break: break-word",
            "Machine-readable catalog",
        ):
            self.assertIn(marker, text)

    def test_homepage_preserves_search_filter_save_sort_recent_and_copy_contract(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        markers = (
            'name="viewport"',
            "Offline Emergency Library",
            "library-search",
            'id="reset-search"',
            'class="document"',
            "offline_library_poc_catalog_documents",
            "document.route",
            "document.asset_route",
            "document.download_route",
            "document.title",
            "document.category",
            "document.description",
            "document.publisher",
            "No documents found",
            "data-document=",
            "Available without internet access",
            "Skip to collection",
            "Source preserved",
            'aria-label="Document categories"',
            "data-category=",
            'data-category=""',
            'aria-pressed="true"',
            "publisher-filter",
            "format-filter",
            "All publishers",
            "All formats",
            "save-resource",
            "saved-toggle",
            "offline-library-saved",
            "catalog-sort",
            "Sort by",
            "active-criteria",
            "active-criterion",
            "renderActiveCriteria",
            "Remove ",
            "recent",
            "recent-bar",
            "recent-title",
            "recent-route",
            "open-recent",
            "clear-recent-bar",
            "offline-library-recent",
            "trackRecent",
            "copy-link",
            "copyLink",
            "copy-fallback",
            "Copy link",
            "data-local-url=",
            "Copy local address",
        )
        for marker in markers:
            self.assertIn(marker, text)
        self.assertIn("document.querySelector", text)
        self.assertIn(".includes(q)", text)
        self.assertIn("localStorage", text)
        self.assertIn("sortCards", text)

    def test_homepage_renders_catalog_metadata_as_local_document_cards(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            "data-title=",
            "data-category=",
            "data-publisher=",
            "data-format=",
            "document.route",
            "document.asset_route",
            "document.download_route",
        ):
            self.assertIn(marker, text)
        self.assertRegex(text, r"resources\s*\|\s*length")

    def test_catalog_contains_required_metadata_for_each_document(self):
        catalog = CATALOG.read_text(encoding="utf-8")
        entries = catalog.split("\n- id: ")[1:]
        self.assertGreaterEqual(len(entries), 3)
        fields = (
            "title:",
            "category:",
            "description:",
            "route:",
            "asset_route:",
            "download_route:",
            "bytes:",
            "sha256:",
            "source_url:",
            "retrieved:",
            "original_filename:",
            "publisher:",
            "format:",
            "redistribution_status:",
        )
        for entry in entries:
            for field in fields:
                self.assertIn(field, entry)

    def test_homepage_has_accessible_navigation_and_offline_notice(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            'href="#main-content"',
            'aria-label="Offline availability"',
            'aria-label="Document categories"',
            'aria-label="Library documents"',
            'id="library-search"',
        ):
            self.assertIn(marker, text)

    def test_template_does_not_require_deployment_role_defaults(self):
        # Host paths, nginx variables, and Ansible defaults are outside this
        # standalone template test; catalog metadata remains in config/catalog.yaml.
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("nginx", text.lower())
        self.assertNotIn("ansible", text.lower())
