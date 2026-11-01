"""Content-manifest policy contract, without deployment assertions."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates/content-manifest.yaml.j2"
DOC = ROOT / "docs/legacy-offline-library-role.md"

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def test_manifest_template_defaults_to_no_bulk_approval() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assert '"bulk_acquisition_approved": false' in template
    assert '"items": {{ items | default([]) | tojson(indent=2) }}' in template


def test_manifest_policy_requires_reviewable_fields_and_safe_sources() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for marker in (
        "contain exactly `id`, `title`, `kind`, `source_url`, `sha256`, `destination`, and",
        "`enabled`. URLs must use HTTPS",
        "checksums must be explicit SHA-256 values",
        "destinations must be traversal-safe paths relative to the storage mount",
        "duplicate source URLs are rejected",
        "performs no network request or\ncontent transfer",
    ):
        assert marker in doc


def test_sha256_policy_shape_is_explicit() -> None:
    assert SHA256.fullmatch("0" * 64)
    assert not SHA256.fullmatch("not-a-checksum")
    assert not SHA256.fullmatch("0" * 63)
