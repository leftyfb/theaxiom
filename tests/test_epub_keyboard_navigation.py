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



def test_epub_reader_has_no_inline_script_under_restrictive_csp():
    html = render(1)
    assert '<script' not in html.lower()
    assert 'data-chapter-nav="previous"' in html
    assert 'data-chapter-nav="next"' in html


def test_epub_reader_mobile_controls_remain_accessible():
    html = render(1)
    assert '@media(max-width:600px)' in html
    assert 'min-height:44px' in html
    assert 'a:focus-visible,summary:focus-visible' in html
    assert 'aria-label="Reading controls"' in html
    assert 'aria-label="Chapter navigation"' in html
    assert 'aria-label="Continue reading"' in html
    assert 'Table of contents' in html
    assert 'Download EPUB' in html
    assert 'Return to library' in html


def test_epub_reader_skip_link_targets_focusable_chapter():
    html = render(1)
    assert 'href="#chapter-content">Skip to chapter text</a>' in html
    assert 'id="chapter-content" tabindex="-1"' in html
    assert '.skip-reading:focus{' in html
    assert '<script' not in html.lower()


def test_collapsed_toc_identifies_current_chapter():
    for chapter, label in enumerate(("First", "Second", "Third")):
        html = render(chapter)
        assert f'<summary>Table of contents <span class="toc-current">— {label}</span></summary>' in html
        assert '<script' not in html.lower()
