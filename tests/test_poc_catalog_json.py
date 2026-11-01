"""Validate the standalone local POC catalog JSON contract."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import yaml

ROOT = Path(__file__).parents[1]


def test_poc_catalog_json_has_starter_documents_and_metadata() -> None:
    resources = yaml.safe_load((ROOT / "config/catalog.yaml").read_text(encoding="utf-8"))["documents"]
    env = Environment(loader=FileSystemLoader(ROOT / "templates"))
    payload = json.loads(env.get_template("poc-catalog.json.j2").render(offline_library_catalog_resources=resources))
    assert len(payload["documents"]) == 3
    for document in payload["documents"]:
        assert document["title"]
        assert document["category"]
        assert document["route"].startswith("/documents/")
        assert document["bytes"] > 0
        assert len(document["sha256"]) == 64
