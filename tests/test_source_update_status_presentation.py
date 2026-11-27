"""Source update status presentation regressions for #106.

These checks intentionally inspect the shipped template because the status is
rendered server-side and updated client-side; neither should duplicate the
single persistent status line.
"""
from pathlib import Path
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "admin-page.html.j2"


class SourceUpdateStatusPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_single_persistent_update_status_element(self):
        self.assertEqual(self.template.count('data-field="discovery-evidence"'), 3)
        self.assertIn('data-field="discovery-evidence" role="status" aria-live="polite"', self.template)

    def test_manual_zim_check_reuses_persistent_status(self):
        start = self.template.index("document.querySelectorAll('[data-zim-update-check], [data-document-update-check]')")
        end = self.template.index("document.querySelectorAll('[data-delete-downloads]')", start)
        handler = self.template[start:end]
        self.assertIn("card.querySelector('[data-field=\"discovery-evidence\"]')", handler)
        self.assertIn("feedback.textContent = 'Checking…'", handler)
        self.assertIn("feedback.textContent = {", handler)
        self.assertIn("if (actionFeedback) actionFeedback.textContent = ''", handler)
        self.assertNotIn("const feedback = card.querySelector('[data-zim-feedback]')", handler)

    def test_technical_discovery_method_not_in_primary_status(self):
        start = self.template.index('data-field="discovery-status"')
        end = self.template.index('data-field="discovery-evidence"', start)
        primary = self.template[start:end]
        self.assertIn("Update policy:", primary)
        self.assertNotIn("Kiwix OPDS / Metalink", primary)
        technical = self.template[self.template.index('<details class="source-technical">'):]
        self.assertIn("Update discovery:", technical)
        self.assertIn("Kiwix OPDS / Metalink", technical)

    def test_refresh_does_not_overwrite_inflight_check(self):
        self.assertIn("if (card.dataset.updateCheckPosting !== 'true') evidence.textContent", self.template)


if __name__ == "__main__":
    unittest.main()
