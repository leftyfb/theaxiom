"""EPUB keyboard navigation and chapter boundary regressions (#83)."""
from pathlib import Path

from jinja2 import Environment


TEMPLATE = Environment(autoescape=True).from_string(
    (Path(__file__).resolve().parents[1] / "templates" / "epub-reader.html.j2").read_text()
)


def render(chapter):
    class Profile:
        title = "Example book"
        author = "Example author"

    class Record:
        profile = Profile()

    return TEMPLATE.render(record=Record(), base="/epub/example", chapter=chapter,
                           size="medium", labels=["First", "Second", "Third"], content="<p>Body</p>")


def test_epub_keyboard_navigation_has_boundary_aware_links():
    first = render(0)
    middle = render(1)
    last = render(2)
    nav = lambda page: page.split('<nav aria-label="Chapter navigation">', 1)[1].split("</nav>", 1)[0]
    assert 'data-chapter-nav="previous"' not in nav(first)
    assert 'data-chapter-nav="next"' in nav(first)
    assert 'data-chapter-nav="previous"' in nav(middle)
    assert 'data-chapter-nav="next"' in nav(middle)
    assert 'data-chapter-nav="previous"' in nav(last)
    assert 'data-chapter-nav="next"' not in nav(last)


def test_epub_keyboard_navigation_ignores_editing_and_shortcuts():
    html = render(1)
    assert "event.defaultPrevented" in html
    for modifier in ("altKey", "ctrlKey", "metaKey", "shiftKey"):
        assert "event." + modifier in html
    for control in ("input", "textarea", "select", "button", "[contenteditable]", '[role="textbox"]'):
        assert control in html
    assert "target.isContentEditable" in html
    assert "event.preventDefault()" in html
    assert "window.location.assign(link.href)" in html
