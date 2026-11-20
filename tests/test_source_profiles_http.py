"""Source profile regression tests through the real HTTP handler and template."""
import http.client
import json
import re
import threading
from unittest.mock import Mock

import pytest

from theaxiom import admin_api, catalog_store, publication, storage_settings
from test_admin_page_template import Parser


SID = "wikipedia-en"
PIN = admin_api.WIKIPEDIA_SOURCE


@pytest.fixture
def app(tmp_path, monkeypatch):
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    monkeypatch.setattr(admin_api, "SOURCE_CATALOG", store)
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    monkeypatch.setattr(publication, "reachable", lambda route: True)
    monkeypatch.setattr(publication, "reader_location", lambda route: "/wikipedia/viewer#fixture/")
    snapshot = {"stale": False, "acquisitions": [{
        "source_id": SID, "state": "completed", "active": False, "verified": True,
        "filename": PIN["filename"], "expected_bytes": PIN["bytes"], "sha256": PIN["sha256"],
    }]}
    with admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler) as server:
        server.acquisition_monitor = Mock()
        server.acquisition_monitor.snapshot.return_value = snapshot
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, payload=None, status=200):
            host, port = server.server_address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                if payload is None:
                    conn.request("GET", path)
                else:
                    conn.request("POST", path, json.dumps(payload), {
                        "Content-Type": "application/json", "Origin": f"http://{host}:{port}",
                    })
                response = conn.getresponse()
                body = response.read().decode()
                assert response.status == status, body
                if response.getheader("Content-Type", "").startswith("application/json"):
                    return json.loads(body)
                return response.getheader("Location") if status == 302 else body
            finally:
                conn.close()

        try:
            yield request, snapshot
        finally:
            server.shutdown()
            thread.join(timeout=5)


def source(catalog, sid=SID):
    return next(item for item in catalog["sources"] if item["source_id"] == sid)


def mutate(request, catalog, operation, fields=None, sid=SID, status=200):
    return request("/api/sources", dict(operation=operation, source_id=sid,
                                        fields=fields or {}, revision=catalog["revision"]), status=status)


def publish(request, enabled, status=200):
    return request("/api/publication", dict(source_id=SID, enabled=enabled), status=status)


def card(page, sid=SID):
    return next((item for item in re.findall(r'<details class="source-card"[^>]*>.*?</details>', page, re.S)
                 if f'data-source-id="{sid}"' in item), None)


def control(html, key, value):
    parser = Parser()
    parser.feed(html)
    return next(attrs for _, attrs in parser.tags if attrs.get(key) == value)


def test_published_profile_edit_unpublish_remove_and_equivalent_readd(app, tmp_path):
    request, _ = app
    archive = tmp_path / PIN["filename"]
    archive.write_bytes(b"installed fixture bytes")
    publication.save(SID, True)  # Existing deployment state, before any profile modification.
    initial = request("/api/sources")
    original = source(initial)
    assert original["publication"]["published"]
    page = request("/admin/sources")
    assert 'name="format"' in page
    assert 'data-catalog-format="zim"' in card(page)
    assert "Unpublish" in card(page)
    assert "disabled" in control(card(page), "data-catalog-remove", SID)
    assert request("/read/wikipedia-en", status=302) == "/wikipedia/viewer#fixture/"
    assert request("/internal/publication/wikipedia-en", status=204) == ""
    assert 'href="/read/wikipedia-en"' in request("/")
    mutate(request, initial, "remove", status=409)
    mutate(request, initial, "edit", {"format": "pdf"}, status=409)
    edited = mutate(request, initial, "edit", {"title": 'Local <reference> "edition"', "license": "Operator note"})
    assert source(edited)["artifact"] == original["artifact"]
    assert source(edited)["publication"]["published"]
    wiki = card(request("/admin/sources"))
    assert "Local &lt;reference&gt;" in wiki and "Candidate license</dt><dd>Operator note" in wiki
    publish(request, False)
    assert request("/read/wikipedia-en", status=403)
    before = card(request("/admin/sources"))
    assert "disabled" not in control(before, "data-catalog-remove", SID)
    assert "disabled" not in control(before, "data-field", "publication-toggle")
    removed = mutate(request, edited, "remove")
    assert card(request("/admin/sources")) is None
    fields = source(edited)["profile"] | source(edited)["configuration"]
    added = mutate(request, removed, "add", fields)
    assert card(request("/admin/sources")) == before
    assert source(added)["publication"]["serving"] and not source(added)["publication"]["enabled"]
    publish(request, True)
    assert request("/read/wikipedia-en", status=302) == "/wikipedia/viewer#fixture/"
    assert archive.read_bytes() == b"installed fixture bytes"


@pytest.mark.parametrize("conflict", [dict(format="pdf"), dict(source_url="https://example.org/a.zim"),
                                      dict(expected_size_bytes="1"), dict(sha256="a" * 64)])
def test_conflicting_profile_does_not_inherit_trusted_state(app, conflict):
    request, _ = app
    catalog = request("/api/sources")
    edited = mutate(request, catalog, "edit", conflict)
    wiki = source(edited)
    assert all(wiki[key] is None for key in ("artifact", "acquisition", "publication"))
    assert wiki["configuration"] == source(catalog)["configuration"] | conflict
    changed = card(request("/admin/sources"))
    assert "Candidate SHA-256" in changed and "Pinned archive" not in changed
    assert "publication-toggle" not in changed and "Not confirmed" in changed
    publish(request, True, status=409)
    publication.save(SID, True)  # A stale persisted flag cannot bypass the configuration check.
    assert request("/internal/publication/wikipedia-en", status=403) == ""
    assert request("/read/wikipedia-en", status=403)
    assert "Unpublish" in card(request("/admin/sources"))


def test_monitor_identity_mismatch_does_not_supply_verification(app):
    request, snapshot = app
    snapshot["acquisitions"][0]["sha256"] = "b" * 64
    wiki = source(request("/api/sources"))
    assert wiki["artifact"]["sha256"] == PIN["sha256"]
    assert wiki["acquisition"] is None and not wiki["publication"]["verified"]
    publish(request, True, status=409)


def test_empty_catalog_and_same_candidate_card_after_manual_readd(app):
    request, _ = app
    catalog = request("/api/sources")
    sid = "internet-archive"
    seed = source(catalog, sid)
    before = card(request("/admin/sources"), sid)
    for item in catalog["sources"]:
        catalog = mutate(request, catalog, "remove", sid=item["source_id"])
    page = request("/admin/sources")
    assert "No managed sources configured." in page and 'class="source-card"' not in page
    assert request("/api/sources")["sources"] == []
    catalog = mutate(request, catalog, "add", seed["profile"] | seed["configuration"], sid=sid)
    assert card(request("/admin/sources"), sid) == before
    mutate(request, catalog, "edit", {"format": "pdf", "source_url": "https://example.org/guide.pdf"}, sid=sid)
    edited = card(request("/admin/sources"), sid)
    assert 'data-catalog-format="pdf"' in edited
    assert 'data-catalog-source-url="https://example.org/guide.pdf"' in edited


@pytest.mark.parametrize("state,active", [("downloading", True), ("unknown", False)])
def test_active_and_unresolved_states_block_seed_and_manual_changes(app, state, active):
    request, snapshot = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {"title": "Manual"}, sid="manual")
    for sid in (SID, "manual"):
        snapshot["acquisitions"].append(dict(source_id=sid, state=state, active=active))
        rendered = card(request("/admin/sources"), sid)
        assert source(request("/api/sources"), sid)["mutation_blocked"]
        for action in ("edit", "remove"):
            assert "disabled" in control(rendered, f"data-catalog-{action}", sid)
            mutate(request, catalog, action, {"title": "Changed"} if action == "edit" else {},
                   sid=sid, status=400)
