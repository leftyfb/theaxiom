import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "templates" / "admin-page.html.j2"


class AdminWebUISkeletonTests(unittest.TestCase):
    def test_admin_template_is_standalone_html_with_accessible_shell(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            "<!doctype html>",
            'lang="en"',
            'meta name="viewport"',
            "Skip to admin table",
            'id="main-content"',
            "Manage offline sources",
        ):
            self.assertIn(marker, text)
        self.assertNotIn("nginx", text.lower())
        self.assertNotIn("systemd", text.lower())

    def test_template_exposes_zim_actions_and_source_metadata(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            'data-action="download-zim"',
            'data-action="redownload-zim"',
            'data-action="update-zim"',
            'data-source-id="wikipedia-en"',
            "data-url=",
            "data-dest=",
            "data-bytes=",
            "data-sha256=",
        ):
            self.assertIn(marker, text)

    def test_template_handles_empty_and_manifest_backed_states(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("No managed sources configured.", text)
        self.assertIn("offline_library_content_manifest", text)
        self.assertIn("offline_library_kiwix_zim_enabled", text)

    def test_inline_script_posts_bounded_job_requests(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("fetch('/api/action'", text)
        self.assertIn("fetch(`/api/jobs/${jobId}`)", text)
        self.assertIn("maxAttempts", text)
        self.assertRegex(text, r"\^\[a-f0-9\]\{32\}\$.*job_id")

    def test_action_payload_is_json_and_uses_source_id(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("'Content-Type': 'application/json'", text)
        self.assertIn("JSON.stringify({ source_id: row.dataset.sourceId, action })", text)

    def test_deployment_specific_auth_and_proxy_assertions_are_not_required(self):
        # The standalone repository owns the page and API contract, not nginx
        # proxy/auth configuration from the original Ansible role.
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("auth_basic", text)
        self.assertNotIn("proxy_pass", text)
