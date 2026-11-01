"""Catalog template preserves content metadata and provenance."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import yaml

ROOT = Path(__file__).parents[1]


def resources() -> list[dict]:
    return yaml.safe_load((ROOT / "config/catalog.yaml").read_text(encoding="utf-8"))["documents"]


def test_catalog_contains_all_starter_titles_categories_routes_sizes_and_checksums() -> None:
    expected = {
        "Emergency Disinfection of Drinking Water": ("Water", 754723, "f404083d00decedc267bf4265eda776bb250ccfe18c1f7b19152aa006ff727c5"),
        "Handwashing: Clean Hands Save Lives": ("Sanitation", 30688394, "b7095c42196a6b63e91210c5fe2c7c9d3792d5f95718e3bf137270d18f533bc8"),
        "Power Outages": ("Emergency Preparedness", 317550, "d98a6ca01ba91c8b5a6d4772ce46dec7ecc8481eab5778373b5d1b54e49f4697"),
    }
    catalog = {item["title"]: item for item in resources()}
    assert set(catalog) == set(expected)
    for title, (category, size, checksum) in expected.items():
        item = catalog[title]
        assert (item["category"], item["bytes"], item["sha256"]) == (category, size, checksum)
        assert item["route"].startswith("/documents/")
        assert item["asset_route"].startswith("/content/")
        assert item["download_route"].startswith("/download/")


def test_catalog_template_renders_machine_readable_json() -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"))
    rendered = env.get_template("poc-catalog.json.j2").render(offline_library_catalog_resources=resources())
    payload = json.loads(rendered)
    assert [item["title"] for item in payload["documents"]] == [item["title"] for item in resources()]
    assert all({"publisher", "source_url", "retrieved", "original_filename", "format", "redistribution_status"} <= item.keys() for item in payload["documents"])
