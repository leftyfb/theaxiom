#!/usr/bin/env python3
"""Focused responsive, keyboard, and print contract checks for the homepage."""
from pathlib import Path

text = (Path(__file__).parents[1] / "templates/kiwix-homepage.html.j2").read_text()
for marker in (
    'box-sizing: border-box', 'width: min(100% - 2rem, 70rem)',
    'min-height: 44px', '@media (max-width: 700px)', '@media (max-width: 480px)',
    'flex-wrap: wrap', 'aria-label="Library documents"', 'aria-hidden="true"',
    'type="search"', 'href="{{ document.route }}"', ':focus-visible',
    '@media print', 'overflow-x: auto', 'grid-template-columns: 1fr',
    'save-resource', 'saved-toggle', 'aria-pressed', 'localStorage',
    'id="print-collection"', 'Print index', 'print-route', 'window.print()',
    'copy-link', 'copyLink', 'copy-fallback', 'Copy link',
):
    assert marker in text, f"responsive contract missing: {marker}"
assert 'overflow-x: hidden' not in text, "homepage must not mask horizontal overflow"
print("homepage responsive, keyboard, and print layout contract: PASS")
