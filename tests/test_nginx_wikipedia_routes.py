from pathlib import Path


SITE = Path(__file__).resolve().parents[1] / "deploy/nginx/theaxiom.conf"


def test_legacy_wikipedia_deep_routes_reach_dynamic_backend():
    text = SITE.read_text(encoding="utf-8")
    block = text.split("location ^~ /wikipedia/ {", 1)[1].split("    # Other content routes", 1)[0]
    assert "include /opt/theaxiom/app/templates/theaxiom-backend.conf;" in block
    assert "rewrite" not in block
    assert "return 302 /zim" not in block
    assert "wikipedia_en_all_maxi_2026-08" not in block


def test_legacy_wikipedia_entry_roots_remain_safe_static_redirects():
    text = SITE.read_text(encoding="utf-8")
    assert "location = /wikipedia { return 302 /; }" in text
    assert "location = /wikipedia/ { return 302 /; }" in text


def test_dynamic_route_precedes_catch_all_and_does_not_bypass_backend_gate():
    text = SITE.read_text(encoding="utf-8")
    wikipedia = text.index("location ^~ /wikipedia/ {")
    catch_all = text.index("location / {", wikipedia)
    assert wikipedia < catch_all
    assert text[wikipedia:catch_all].count("theaxiom-backend.conf") == 1
