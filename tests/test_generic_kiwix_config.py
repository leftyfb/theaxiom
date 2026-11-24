"""Static fail-closed wiring contracts for optional generic Kiwix deployment."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_optional_generic_kiwix_unit_requires_reader_acknowledgement():
    unit = (ROOT / "templates/kiwix-serve.service").read_text()
    assert not (ROOT / "templates/theaxiom-kiwix.service").exists()
    assert "User=theaxiom" in unit
    assert "RuntimeDirectory=theaxiom-kiwix" in unit
    assert "ExecStartPre=/usr/bin/rm -f /run/theaxiom-kiwix/manifest.sha256" in unit
    assert "ExecStartPre=/opt/theaxiom/app/.venv/bin/python -m theaxiom.zim_catalog" in unit
    assert "ExecStartPost=/opt/theaxiom/app/.venv/bin/python -m theaxiom.zim_reader_probe" in unit
    assert "--address=127.0.0.1 --port=8081" in unit
    assert "--monitorLibrary --library" in unit
    assert "--library /var/lib/theaxiom/kiwix-library.xml" in unit
    assert "chmod" not in unit.lower() and "chown" not in unit.lower()


def test_nginx_kiwix_proxy_requires_internal_publication_for_all_paths():
    config = (ROOT / "deploy/nginx/theaxiom.conf").read_text()
    location = config.split("location ^~ /zim/ {", 1)[1].split("}", 1)[0]
    assert "auth_request /_publication_zim;" in location
    assert "proxy_pass http://127.0.0.1:8081;" in location
    assert 'proxy_set_header Authorization "";' in location
    guard = config.split("location = /_publication_zim {", 1)[1].split("}", 1)[0]
    assert "internal;" in guard
    assert "X-Axiom-Original-URI $request_uri" in guard
    assert "proxy_pass http://127.0.0.1:8080/internal/publication/zim;" in guard
    assert 'location ~ "^/read/zim/[a-z0-9-]{1,63}$"' in config


def test_nginx_exposes_only_the_shared_publication_gated_zim_reader():
    config = (ROOT / "deploy/nginx/theaxiom.conf").read_text()
    assert "/wikipedia" not in config
    assert "_publication_wikipedia" not in config
    assert "location ^~ /zim/ {" in config
    assert "auth_request /_publication_zim;" in config
    assert "proxy_pass http://127.0.0.1:8082;" not in config



def test_shared_kiwix_viewer_has_library_return_for_all_zim_sources():
    config = (ROOT / "deploy/nginx/theaxiom.conf").read_text()
    viewer = config.split("location = /zim/viewer {", 1)[1].split("\n    }", 1)[0]
    assert "auth_request /_publication_zim;" in viewer
    assert "proxy_pass http://127.0.0.1:8081;" in viewer
    assert 'proxy_set_header Accept-Encoding "";' in viewer
    assert "sub_filter_once on;" in viewer
    assert 'id="kiwix_serve_taskbar_library_button"' in viewer
    assert 'href="/"' in viewer
    assert "Back to library" in viewer
    assert "location ^~ /zim/ {" in config
