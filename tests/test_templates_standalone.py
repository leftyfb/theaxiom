"""Ensure application templates are ordinary Jinja templates, not Ansible templates."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"


def test_all_templates_compile_with_jinja_without_ansible_filters() -> None:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=True,
    )
    for template_path in sorted(TEMPLATE_DIR.glob("*.j2")):
        assert environment.get_template(template_path.name) is not None


def test_acquisition_templates_render_json_compatible_yaml() -> None:
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    manifest = environment.get_template("content-manifest.yaml.j2").render(
        items=[{"id": "sample", "enabled": True}]
    )
    plan = environment.get_template("acquisition-plan.yaml.j2").render(
        content_manifest=[{"id": "sample", "enabled": True}]
    )
    assert json.loads(manifest)["items"][0]["id"] == "sample"
    assert json.loads(plan)["content"][0]["id"] == "sample"
