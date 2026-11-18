from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment
from theaxiom.admin_api import WIKIPEDIA_SOURCE

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def render(**overrides):
    values = dict(offline_library_content_manifest=[], offline_library_kiwix_zim_enabled=True,
                  offline_library_kiwix_zim=WIKIPEDIA_SOURCE)
    values.update(overrides)
    return Environment(autoescape=True).from_string(TEMPLATE.read_text()).render(**values)


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_source_card_and_disabled_actions():
    parser = Parser()
    parser.feed(render())
    assert sum(tag == "article" and attrs.get("data-source-id") == "wikipedia-en"
               for tag, attrs in parser.tags) == 1
    actions = [attrs for tag, attrs in parser.tags if tag == "button" and "data-action" in attrs]
    assert {a["data-action"] for a in actions} == {"download-zim", "redownload-zim", "update-zim"}
    assert all("disabled" in a for a in actions)
    assert all("open" not in attrs for tag, attrs in parser.tags if tag == "details")
    verify = next(attrs for tag, attrs in parser.tags if attrs.get("data-field") == "verification")
    assert "value" not in verify


def test_manifest_and_empty_cards():
    page = render(offline_library_kiwix_zim_enabled=False,
                  offline_library_content_manifest=[{"title": "Fixture", "kind": "pdf"}])
    assert "Fixture" in page and "Status unavailable" in page
    assert "No managed sources configured." in render(offline_library_kiwix_zim_enabled=False)
