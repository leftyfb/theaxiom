"""Standalone acquisition-plan policy and rendering contract."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "templates"
CONFIG = ROOT / "config/acquisition.example.yaml"


def test_acquisition_plan_is_explicitly_approved_and_transfer_free() -> None:
    template = (TEMPLATES / "acquisition-plan.yaml.j2").read_text(encoding="utf-8")
    assert '"bulk_acquisition_approved": true' in template
    assert '"transfer_performed": false' in template
    assert "selectattr('enabled', 'equalto', true)" in template


def test_example_manifest_keeps_bulk_acquisition_disabled() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    assert "bulk_acquisition_approved: false" in config
    assert "items: []" in config


def test_enabled_entries_render_as_json_plan_content() -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATES))
    rendered = env.get_template("acquisition-plan.yaml.j2").render(
        content_manifest=[
            {"id": "disabled", "enabled": False},
            {"id": "approved", "enabled": True},
        ]
    )
    plan = json.loads(rendered)
    assert plan == {
        "bulk_acquisition_approved": True,
        "transfer_performed": False,
        "content": [{"id": "approved", "enabled": True}],
    }
