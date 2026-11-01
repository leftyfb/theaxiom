import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from jinja2 import Environment

TEMPLATE = Path(__file__).parents[1] / "templates" / "admin-page.html.j2"


class WikipediaRowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.depth = 0
        self.download_buttons = []
        self.actions = []
        self.status_markup = []
        self.current_button = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr" and attributes.get("data-zim-row") == "true":
            self.in_row = attributes.get("data-source-id") == "wikipedia-en"
            self.depth = 1
        elif self.in_row and tag == "tr":
            self.depth += 1
        if self.in_row and tag == "button":
            if attributes.get("data-action"):
                self.actions.append(attributes["data-action"])
            if attributes.get("data-action") == "download-zim":
                self.current_button = {"text": "", "attributes": attributes}
        if (
            self.in_row
            and tag == "span"
            and "action-status" in (attributes.get("class") or "").split()
        ):
            self.status_markup.append(attributes)

    def handle_data(self, data):
        if self.current_button is not None:
            self.current_button["text"] += data

    def handle_endtag(self, tag):
        if tag == "button" and self.current_button is not None:
            self.download_buttons.append(self.current_button)
            self.current_button = None
        elif tag == "tr" and self.in_row:
            self.depth -= 1
            if self.depth == 0:
                self.in_row = False


def render_template():
    environment = Environment()
    environment.filters["bool"] = bool
    return environment.from_string(TEMPLATE.read_text(encoding="utf-8")).render(
        offline_library_content_manifest=[],
        offline_library_kiwix_zim_enabled=True,
        offline_library_kiwix_zim={
            "title": "Wikipedia",
            "catalog_date": "",
            "source_url": "https://example.invalid/source.zim",
            "filename": "source.zim",
            "bytes": 1,
            "sha256": "0" * 64,
        },
        offline_library_storage_mount="/tmp/theaxiom",
    )


class AdminPageTemplateTests(unittest.TestCase):
    def test_admin_script_parses_and_posts_to_expected_endpoint(self):
        script = re.search(
            r"<script>(.*?)</script>", TEMPLATE.read_text(encoding="utf-8"), re.DOTALL
        )
        self.assertIsNotNone(script)
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to validate admin JavaScript")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as source:
            source.write(script.group(1))
            source.flush()
            with mock.patch("subprocess.run", wraps=subprocess.run) as run:
                result = subprocess.run(
                    [node, "--check", source.name], capture_output=True, text=True, check=False
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch('/api/action'", script.group(1))
        self.assertRegex(script.group(1), r"method:\s*'POST'")

    def test_rendered_zim_row_has_expected_source_id(self):
        parser = WikipediaRowParser()
        parser.feed(render_template())
        self.assertEqual(len(parser.download_buttons), 1)
        self.assertEqual(parser.download_buttons[0]["text"].strip(), "Download")
        self.assertEqual(len(parser.status_markup), 1)
        self.assertEqual(parser.status_markup[0].get("role"), "status")
        self.assertEqual(parser.status_markup[0].get("aria-live"), "polite")
        self.assertIn("hidden", parser.status_markup[0])

    def _assert_action(self, action):
        rendered = render_template()
        parser = WikipediaRowParser()
        parser.feed(rendered)
        self.assertIn(f"{action}-zim", parser.actions)
        node = shutil.which("node")
        self.assertIsNotNone(node)
        script = re.search(r"<script>(.*?)</script>", rendered, re.DOTALL).group(1)
        harness = f"""
const requests=[]; const button={{dataset:{{action:'{action}-zim'}},textContent:'{action}',closest:()=>({{dataset:{{sourceId:'wikipedia-en'}}}}),addEventListener:(_n,cb)=>button.click=cb}};
global.document={{querySelectorAll:()=>[button]}}; global.fetch=async(url,options)=>{{requests.push({{url,method:options.method,body:JSON.parse(options.body)}});return {{ok:true,json:async()=>({{skipped:true}})}};}}; global.window={{alert:(m)=>{{throw new Error(m)}}}};
{script}
button.click().then(()=>console.log(JSON.stringify(requests)));
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as source:
            source.write(harness)
            source.flush()
            result = subprocess.run(
                [node, source.name], capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {
                    "url": "/api/action",
                    "method": "POST",
                    "body": {"source_id": "wikipedia-en", "action": action},
                }
            ],
        )

    def test_rendered_redownload_button_posts_redownload_action(self):
        self._assert_action("redownload")

    def test_rendered_update_button_posts_update_action(self):
        self._assert_action("update")

    def test_action_shows_pending_indicator_when_request_is_submitted(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        script = re.search(
            r"<script>(.*?)</script>", TEMPLATE.read_text(encoding="utf-8"), re.DOTALL
        ).group(1)
        harness = f"""const status={{textContent:'',hidden:true}}; const row={{dataset:{{sourceId:'wikipedia-en'}},querySelector:()=>status}}; const button={{dataset:{{action:'update-zim'}},textContent:'Update',closest:()=>row,addEventListener:(_n,cb)=>button.click=cb}}; global.document={{querySelectorAll:()=>[button]}}; global.fetch=async()=>{{console.log(JSON.stringify(status));return {{ok:true,json:async()=>({{skipped:true}})}}}}; global.window={{alert:(m)=>{{throw new Error(m)}}}}; {script} button.click();"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as source:
            source.write(harness)
            source.flush()
            result = subprocess.run(
                [node, source.name], capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"hidden": False, "textContent": "Update request pending…"}
        )
