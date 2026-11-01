"""Exercise admin UI success, failure, and timeout feedback."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEMPLATE = Path(__file__).parents[1] / "templates" / "admin-page.html.j2"


class AdminUIJobStateTests(unittest.TestCase):
    def run_ui(self, job_responses):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to exercise admin JavaScript")
        script = re.search(
            r"<script>(.*?)</script>", TEMPLATE.read_text(encoding="utf-8"), re.DOTALL
        ).group(1)
        harness = r"""
const status = {textContent: '', hidden: true};
const row = {dataset: {sourceId: 'wikipedia-en'}, querySelector: () => status};
const button = {dataset: {action: 'download-zim'}, textContent: 'Download',
  closest: () => row, addEventListener: (_name, callback) => { button.click = callback; }};
global.document = {querySelectorAll: () => [button]};
global.window = {setTimeout: (callback) => callback()};
const jobs = JOB_RESPONSES;
let requestCount = 0;
global.fetch = async (url) => {
  if (url === '/api/action') return {ok: true, json: async () => ({accepted: true, job_id: 'a'.repeat(32)})};
  return {ok: true, json: async () => jobs[requestCount++]};
};
""".replace("JOB_RESPONSES", json.dumps(job_responses))
        harness += script + "\nbutton.click().then(() => console.log(JSON.stringify(status)));\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as source:
            source.write(harness)
            source.flush()
            with mock.patch("subprocess.run", wraps=subprocess.run) as run:
                result = subprocess.run(
                    [node, source.name], capture_output=True, text=True, check=False
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_completed_job_displays_success(self):
        status = self.run_ui([{"status": "completed"}])
        self.assertFalse(status["hidden"])
        self.assertEqual(status["textContent"], "Download completed successfully.")

    def test_failed_job_displays_failure_and_exit_code(self):
        status = self.run_ui([{"status": "failed", "returncode": 7}])
        self.assertEqual(status["textContent"], "Download failed: Acquisition failed (exit 7).")

    def test_pending_jobs_exhausting_poll_limit_display_timeout(self):
        status = self.run_ui([{"status": "pending"}] * 60)
        self.assertEqual(
            status["textContent"],
            "Download failed: Timed out while waiting for the acquisition to finish.",
        )


if __name__ == "__main__":
    unittest.main()
