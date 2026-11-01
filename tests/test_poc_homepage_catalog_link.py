#!/usr/bin/env python3
"""Verify the homepage links to the local machine-readable catalog."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/kiwix-homepage.html.j2"


def main() -> int:
    template = TEMPLATE.read_text()
    assert '<a href="/catalog.json">Machine-readable catalog</a>' in template
    assert '{{ document.route }}' in template
    assert '{{ document.asset_route }}' in template
    assert '{{ document.download_route }}' in template
    assert "http://" not in template and "https://" not in template
    assert template.count("/catalog.json") == 1
    print("POC homepage: document and local catalog links remain offline-capable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
