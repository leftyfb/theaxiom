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

        def request(path, payload=None, status=200, *, raw=False, origin=None, upload_headers=None):
            host, port = server.server_address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                if payload is None:
                    conn.request("GET", path)
                else:
                    headers = {"Content-Type": "application/pdf" if upload_headers is not None else "application/json"}
                    if upload_headers is not None:
                        headers.update(upload_headers)
                    if origin != "":
                        headers["Origin"] = origin or f"http://{host}:{port}"
                    conn.request("POST", path, payload if upload_headers is not None else json.dumps(payload), headers)
                response = conn.getresponse()
                data = response.read()
                if raw:
                    assert response.status == status, data
                    return data, dict(response.getheaders())
                body = data.decode()
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


def publish(request, enabled, status=200, revision=None):
    if revision is None:
        revision = request("/api/sources")["revision"]
    return request("/api/publication", dict(source_id=SID, enabled=enabled, revision=revision), status=status)


def refuse_action(request, catalog, status, sid=SID, action="download", **extra):
    result = request("/api/action", dict(source_id=sid, action=action, revision=catalog["revision"],
                                         **extra), status=status)
    assert result["accepted"] is False
    return result["error"]


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
    assert "revision changed" in refuse_action(request, edited, 409)
    assert "no configured profile" in refuse_action(request, removed, 400)
    publish(request, True, status=409)
    fields = source(edited)["profile"] | source(edited)["configuration"]
    added = mutate(request, removed, "add", fields)
    assert card(request("/admin/sources")) == before
    restored = source(added)
    for key in ("adapter", "configuration_valid", "artifact", "installed_verified", "actions"):
        assert restored[key] == original[key]
    assert restored["installed_verified"] and restored["publication"]["serving"]
    assert not restored["publication"]["enabled"]
    for action in restored["actions"]:
        assert not restored["actions"][action]
        assert "disabled" in control(card(request("/admin/sources")), "data-action", action)
        assert "acquisition disabled" in refuse_action(request, added, 503, action=action)
    publish(request, True, status=409, revision=removed["revision"])
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
    assert not wiki["installed_verified"]
    assert refuse_action(request, edited, 409) == wiki["acquisition_reason"]
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
    assert "unresolved" in refuse_action(request, request("/api/sources"), 409)


@pytest.mark.parametrize("format_name,complete,adapter,reason", [
    ("zim", True, "kiwix", "No trusted acquisition manifest"),
    ("zim", False, "kiwix", "ZIM candidate configuration requires"),
    ("other", True, None, "No lifecycle adapter supports this format"),
])
def test_candidate_lifecycle_http_and_dom(app, format_name, complete, adapter, reason):
    request, _ = app
    catalog = request("/api/sources")
    # Even exact Wikipedia pin values under another ID confer no trust.
    fields = {"title": "Candidate", "format": format_name}
    if complete:
        fields.update(source_url=PIN["source_url"], expected_size_bytes=str(PIN["bytes"]), sha256=PIN["sha256"])
    catalog = mutate(request, catalog, "add", fields, sid="candidate")
    candidate = source(catalog, "candidate")
    assert candidate["adapter"] == adapter
    assert candidate["configuration_valid"] is bool(adapter and complete)
    assert candidate["artifact"] is candidate["publication"] is None
    assert not candidate["installed_verified"] and not candidate["actions"]
    assert reason in refuse_action(request, catalog, 409, sid="candidate")
    assert "accepts no acquisition metadata" in refuse_action(request, catalog, 400, sid="candidate",
                                                             sha256=PIN["sha256"])
    rendered = card(request("/admin/sources"), "candidate")
    assert rendered is not None
    summary, details = rendered.split("</summary>", 1)
    assert 'role="status">Not downloaded</span>' in summary
    assert reason not in summary and reason in details
    parser = Parser()
    parser.feed(rendered)
    assert not any("data-action" in attrs or attrs.get("data-field") == "publication-toggle"
                   for _, attrs in parser.tags)


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
    assert "active or unresolved" in refuse_action(request, catalog, 409)
    publish(request, True, status=409)


def test_acquisition_requires_current_revision_and_fresh_monitor(app):
    request, snapshot = app
    catalog = request("/api/sources")
    for revision in (None, True, "0", -1):
        body = request("/api/action", dict(source_id=SID, action="update", revision=revision), status=400)
        assert "requires a catalog revision" in body["error"]
    snapshot["stale"] = True
    assert "unresolved" in refuse_action(request, catalog, 409)
    assert not source(request("/api/sources"))["installed_verified"]
