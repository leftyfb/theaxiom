"""Validate the repository-local tiny-fixture serving contract."""
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_tiny_fixture_is_static_text_and_not_a_kiwix_artifact() -> None:
    contract = (ROOT / "fixtures/kiwix/poc-serving-contract.yaml").read_text(encoding="utf-8")
    for expected in (
        "service: nginx-static",
        "fixture_source: fixtures/kiwix/tiny.txt",
        "fixture_route: /content/offline-library-fixture",
        "content_type: text/plain",
        "kiwix_artifact: false",
    ):
        assert expected in contract
    assert (ROOT / "fixtures/kiwix/tiny.txt").read_bytes()
    assert "tiny.txt" not in (ROOT / "templates/kiwix-library.xml.j2").read_text(encoding="utf-8")
