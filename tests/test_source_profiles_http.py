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
    monkeypatch.setattr(publication, "reader_location", lambda route, book_id=None: (route["path"] + "viewer#fixture/"))
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
    assert "disabled" not in control(card(page), "data-catalog-remove", SID)
    assert request("/read/wikipedia-en", status=302) == "/zim/viewer#fixture/"
    assert request("/internal/publication/wikipedia-en", status=204) == ""
    assert 'href="/read/wikipedia-en"' in request("/")
    mutate(request, initial, "edit", {"format": "pdf"}, status=409)
    edited = mutate(request, initial, "edit", {"title": 'Local <reference> "edition"', "license": "Operator note"})
    assert source(edited)["artifact"] == original["artifact"]
    assert source(edited)["publication"]["published"]
    wiki = card(request("/admin/sources"))
    assert "Local &lt;reference&gt;" in wiki and "License</dt><dd>Operator note" in wiki
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
        assert 'data-action="' + action + '"' not in card(request("/admin/sources"))
        assert "acquisition disabled" in refuse_action(request, added, 503, action=action)
    publish(request, True, status=409, revision=removed["revision"])
    publish(request, True)
    assert request("/read/wikipedia-en", status=302) == "/zim/viewer#fixture/"
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
    assert "Expected SHA-256" in changed and "Artifact SHA-256" not in changed
    assert "publication-toggle" not in changed
    assert "Installed verification" not in changed
    assert 'data-field="verified"' not in changed
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
    ("zim", True, "kiwix", "Use the ZIM Download action"),
    ("zim", False, "kiwix", "ZIM download requires a direct plain HTTPS URL"),
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
    assert candidate["artifact"] is None
    if adapter == "kiwix":
        assert candidate["publication"]["verified"] is False
        assert candidate["publication"]["published"] is False
    else:
        assert candidate["publication"] is None
    assert not candidate["installed_verified"] and not candidate["actions"]
    assert reason in refuse_action(request, catalog, 409, sid="candidate")
    assert "accepts no acquisition metadata" in refuse_action(request, catalog, 400, sid="candidate",
                                                             sha256=PIN["sha256"])
    rendered = card(request("/admin/sources"), "candidate")
    assert rendered is not None
    summary, details = rendered.split("</summary>", 1)
    assert ('role="status">Not yet supported</span>' if not adapter else 'role="status">Not downloaded</span>') in summary
    assert reason not in summary
    if complete and adapter == "kiwix":
        assert 'data-zim-download' in details
    else:
        assert reason in details
    parser = Parser()
    parser.feed(rendered)
    assert not any("data-action" in attrs for _, attrs in parser.tags)
    toggles = [attrs for _, attrs in parser.tags if attrs.get("data-field") == "publication-toggle"]
    if adapter == "kiwix":
        assert len(toggles) == 1 and "disabled" in toggles[0]
    else:
        assert toggles == []


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


def test_remove_published_zim_automatically(app, tmp_path, monkeypatch):
    request, snapshot = app
    publication.save(SID, True)
    catalog = request("/api/sources")
    assert source(catalog)["publication"]["published"]
    mutate(request, dict(catalog, revision=catalog["revision"] + 1), "remove", status=409)
    mutate(request, catalog, "remove", {"title": "invalid"}, status=400)
    assert publication.load()[SID]
    snapshot["acquisitions"][0].update(state="downloading", active=True)
    mutate(request, catalog, "remove", status=400)
    assert publication.load()[SID]
    snapshot["acquisitions"][0].update(state="completed", active=False)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("fixture failure")
        patch.setattr(publication, "save", fail)
        mutate(request, catalog, "remove", status=409)
        assert publication.load()[SID]
        assert source(request("/api/sources"))["publication"]["published"]
    with monkeypatch.context() as patch:
        patch.setattr(admin_api.SOURCE_CATALOG, "mutate", fail)
        result = mutate(request, catalog, "remove", status=409)
        assert "unpublished but profile removal failed" in result["error"]
    assert not publication.load()[SID]
    assert request("/internal/publication/wikipedia-en", status=403) == ""
    publish(request, True)
    removed = mutate(request, catalog, "remove")
    assert not any(row["source_id"] == SID for row in removed["sources"])
    assert not publication.load()[SID]
    assert request("/read/wikipedia-en", status=403)
    assert request("/internal/publication/wikipedia-en", status=403) == ""


def test_group_api_bulk_membership_and_revision(app):
    request, _ = app
    catalog = request("/api/sources")
    payload = dict(operation="create", kind="categories", group_id="reference", name="Reference",
                   revision=catalog["revision"])
    request("/api/groups", payload, status=403, origin="")
    created = request("/api/groups", payload)
    assert next(g for g in created["groups"] if g["kind"] == "categories" and g["group_id"] == "reference")["name"] == "Reference"
    request("/api/groups", payload, status=409)
    add = dict(operation="add-members", kind="categories", group_id="reference",
               source_ids=[SID], revision=created["revision"])
    saved = request("/api/groups", add)
    assert next(g for g in saved["groups"] if g["kind"] == "categories" and g["group_id"] == "reference")["source_ids"] == [SID]
    assert request("/api/sources")["groups"] == saved["groups"]
    request("/api/groups", dict(add, source_ids=["missing"], revision=saved["revision"]), status=400)


def test_public_group_filters_hide_unpublished_members(app):
    request, _ = app
    publication.save(SID, True)
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {"title": "Private fixture"}, sid="private-fixture")
    for kind, gid, members in [
        ("categories", "reference", [SID]),
        ("collections", "private", ["private-fixture"]),
        ("tags", "mixed", [SID, "private-fixture"]),
    ]:
        created = request("/api/groups", dict(operation="create", kind=kind, group_id=gid,
                                               name=gid.title(), revision=catalog["revision"]))
        catalog = request("/api/groups", dict(operation="add-members", kind=kind, group_id=gid,
                                               source_ids=members, revision=created["revision"]))
    page = request("/?category=reference&tag=mixed")
    assert 'href="/read/wikipedia-en"' in page
    assert 'Private fixture' not in page and 'value="private"' not in page
    assert 'value="reference" selected' in page
    assert 'href="/read/wikipedia-en"' not in request("/?collection=private")
    assert "No published sources match" in request("/?category=missing")
    request("/?category=reference&category=other", status=400)
    request("/?tag=../bad", status=400)
    assert "Wikipedia" in request("/search?q=water&tag=mixed")
    assert "Wikipedia" not in request("/search?q=water&tag=missing")
    publish(request, False)
    assert 'value="reference"' not in request("/")


def test_admin_filter_status_distinguishes_installed_and_unpublished(app):
    request, snapshot = app
    publication.save(SID, True)
    catalog = request("/api/sources")
    assert source(catalog)["publication_filter"] == "published"
    assert source(catalog)["download_filter"] == "downloaded"
    publish(request, False)
    catalog = request("/api/sources")
    assert source(catalog)["publication_filter"] == "unpublished"
    assert source(catalog)["download_filter"] == "downloaded"
    catalog = mutate(request, catalog, "add", {"title": "Not downloaded"}, sid="candidate-filter")
    assert source(catalog, "candidate-filter")["download_filter"] == "not-downloaded"
    snapshot["stale"] = True
    assert source(request("/api/sources"))["download_filter"] == "unknown"


def test_bulk_group_assignment_preserves_unrelated_memberships(app):
    request, _ = app
    state = request("/api/sources")
    state = mutate(request, state, "add", {"title": "Second"}, sid="second")
    def apply(operation, kind, ids, sources, status=200):
        return request("/api/groups", dict(operation=operation, kind=kind, group_id="bulk",
                       group_ids=ids, source_ids=sources, revision=state["revision"]), status=status)
    state = apply("bulk-add", "categories", ["communications", "energy-power"], [SID, "second"])
    state = apply("bulk-add", "tags", ["winter"], [SID])
    state = apply("bulk-remove", "categories", ["communications"], [SID])
    state = apply("bulk-replace", "categories", ["general-reference"], [SID])
    groups = {(g["kind"], g["group_id"]): g["source_ids"] for g in state["groups"]}
    assert groups["categories", "communications"] == ["second"]
    assert groups["categories", "energy-power"] == ["second"]
    assert groups["categories", "general-reference"] == [SID]
    assert groups["tags", "winter"] == [SID]
    apply("bulk-replace", "categories", ["missing"], [SID], status=400)
    assert request("/api/sources")["revision"] == state["revision"]
    state = apply("bulk-replace", "categories", [], [SID])
    assert all(SID not in g["source_ids"] for g in state["groups"] if g["kind"] == "categories")


def test_zim_preflight_error_returns_actionable_message(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    def missing(*args):
        raise FileNotFoundError("private path")
    monkeypatch.setattr(admin_api.zim_jobs, "start", missing)
    body = request("/api/action", dict(source_id="fema-ready", action="zim-download",
                                       revision=catalog["revision"]), status=503)
    assert "storage root or staging/library" in body["error"]
    assert "private path" not in body["error"]


def test_generic_zim_publication_intent_waits_for_verified_reader(app, monkeypatch):
    request, _ = app
    sid = "fema-ready"
    catalog = request("/api/sources")
    assert next(s for s in catalog["sources"] if s["source_id"] == sid)["adapter"] == "kiwix"
    job = {"source_id": sid, "state": "completed", "active": False, "verified": True,
           "configuration": {"format": "zim"}}
    monkeypatch.setattr(admin_api.zim_jobs, "status", lambda identifier: job if identifier == sid else None)
    monkeypatch.setattr(admin_api.zim_jobs, "installed",
                        lambda current, config: {"filename": "fema-ready." + "a"*64 + ".zim",
                                                 "bytes": 123, "sha256": "a"*64} if current else None)
    result = request("/api/publication",
                     dict(source_id=sid, enabled=True, revision=catalog["revision"]), status=409)
    assert "permission" in result["error"].lower()
    result = request("/api/publication", dict(source_id=sid, enabled=True,
                        revision=catalog["revision"], acknowledge_rights=True))
    assert result["enabled"] is True
    updated = request("/api/sources")
    state = next(s for s in updated["sources"] if s["source_id"] == sid)["publication"]
    assert state["enabled"] and state["verified"] and not state["published"]
    assert "Waiting for Kiwix to acknowledge" in state["publication_reason"]
    assert request("/read/zim/fema-ready", status=403)["error"] == "ZIM collection unavailable"
    assert sid in request("/api/publication")
    assert not request("/internal/publication/zim", status=403, raw=True)[0]
    result = request("/api/publication", dict(source_id=sid, enabled=False,
                         revision=updated["revision"]))
    assert result["enabled"] is False



@pytest.mark.parametrize("format_name", ["pdf", "epub", "zim"])
def test_source_card_hides_internal_verification_without_breaking_refresh(app, format_name):
    """Presentation-only removal; backend verification and refresh remain intact."""
    request, _ = app
    catalog = request("/api/sources")
    candidate = next((entry for entry in catalog["sources"]
                      if entry["configuration"].get("format") == format_name), None)
    if candidate is None:
        fields = {"title": "Test " + format_name.upper(), "format": format_name}
        catalog = mutate(request, catalog, "add", fields, sid="test-" + format_name)
        candidate = source(catalog, "test-" + format_name)
    html = card(request("/admin/sources"), candidate["source_id"])
    assert html is not None
    assert "Installed verification" not in html
    assert 'data-field="verified"' not in html
    assert "pin verified" not in html
    assert "Artifact SHA-256" in html or "Technical details" in html
    # GET /api/sources still supplies verification state used by publication gates.
    persisted = source(request("/api/sources"), candidate["source_id"])
    assert "installed_verified" in persisted


def test_admin_source_refresh_never_updates_removed_verification_field(app):
    request, _ = app
    page = request("/admin/sources")
    assert "Installed verification" not in page
    assert 'data-field="verified"' not in page
    assert "pin verified" not in page
    assert "put('verified'" not in page
    assert "querySelector('[data-field=\\\"verified\\\"]')" not in page
    assert "data-field=\"status\"" in page


def test_zim_restart_requires_explicit_confirmation_at_http_boundary(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    revision = catalog["revision"]
    calls = []

    def start(sid, config, *, restart=False):
        calls.append((sid, restart))
        return {"accepted": True, "state": "queued"}

    monkeypatch.setattr(admin_api.zim_jobs, "start", start)
    base = dict(source_id="fema-ready", action="zim-restart", revision=revision)
    assert request("/api/action", base, status=400)["error"]
    assert request("/api/action", dict(base, confirm_restart=False), status=400)["error"]
    assert request("/api/action", dict(base, confirm_restart="true"), status=400)["error"]
    assert request("/api/action", dict(base, confirm_restart=True, extra=True), status=400)["error"]
    assert calls == []
    result = request("/api/action", dict(base, confirm_restart=True), status=202)
    assert result["accepted"]
    assert calls == [("fema-ready", True)]


@pytest.mark.parametrize("action,method,keyword", [
    ("zim-retry", "start", "retry"),
    ("zim-cancel-keep", "control", "cancel-keep"),
])
def test_zim_recovery_actions_dispatch_through_http(app, monkeypatch, action, method, keyword):
    request, _ = app
    revision = request("/api/sources")["revision"]
    calls = []
    if method == "start":
        def start(sid, config, *, retry=False):
            calls.append((sid, retry))
            return {"accepted": True, "state": "queued"}
        monkeypatch.setattr(admin_api.zim_jobs, "start", start)
    else:
        def control(sid, config, operation):
            calls.append((sid, operation))
            return {"accepted": True, "state": "paused"}
        monkeypatch.setattr(admin_api.zim_jobs, "control", control)
    result = request("/api/action", dict(source_id="fema-ready", action=action,
                                         revision=revision), status=202)
    assert result["accepted"]
    assert calls == [("fema-ready", True if method == "start" else keyword)]


def test_freshness_endpoint_selects_trusted_source_without_wikipedia_default(app, monkeypatch):
    request, _ = app
    second = dict(PIN, source_id="second-zim", title="Second pinned ZIM",
                  source_url="https://download.kiwix.org/zim/second_en_all_2026-11.zim",
                  sha256="b" * 64, bytes=4096)
    checked = []
    monkeypatch.setattr(admin_api, "probe_source",
                        lambda url, digest, size: (checked.append((url, digest, size))
                                                   or {"state": "no_newer_edition_listed"}))
    monkeypatch.setattr(admin_api, "installed_evidence", lambda snapshot, entry: {})
    monkeypatch.setitem(admin_api.TRUSTED_SOURCES, "second-zim", second)
    assert "Specify" in request("/api/freshness", status=400)["error"]
    assert request("/api/freshness?source_id=unknown", status=400)["error"]
    assert request("/api/freshness?source_id=second-zim")["source_id"] == "second-zim"
    assert checked == [(second["source_url"], second["sha256"], second["bytes"])]


def test_storage_status_not_tied_to_default_wikipedia_archive_size(app, monkeypatch):
    request, _ = app
    calls = []
    def inspect(settings, **kwargs):
        calls.append(kwargs)
        return {"ready": False, "error": "mount unavailable"}
    monkeypatch.setattr(admin_api.storage, "inspect_storage", inspect)
    assert request("/api/storage-status")["ready"] is False
    assert calls == [{}]


def test_explicit_kiwix_update_check_requires_same_origin_and_catalog_identity(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {
        "title": "Offline update test", "format": "zim",
        "source_url": PIN["source_url"],
        "expected_size_bytes": str(PIN["bytes"]), "sha256": PIN["sha256"],
    }, sid="update-fixture")
    called = []
    monkeypatch.setattr(admin_api.kiwix_update_discovery, "refresh_checked_source",
                        lambda sid: (called.append(sid) or {
                            "state": "unable_to_determine", "evidence": "fixture has no installed content",
                        }))
    payload = {"source_id": "update-fixture", "revision": catalog["revision"]}
    assert "same-origin" in request("/api/update-check", payload, status=403, origin="")["error"]
    assert "revision" in request("/api/update-check",
        dict(payload, revision=0), status=409)["error"]
    assert "not support" in request("/api/update-check",
        dict(payload, source_id="missing"), status=400)["error"]
    # Pinned legacy acquisition uses a separate verification-gated, read-only
    # checker. Mock it rather than making a live publisher HTTP request.
    pinned_calls = []
    monkeypatch.setattr(admin_api.kiwix_update_discovery, "refresh_pinned_seed",
        lambda record, artifact, *, verified: (
            pinned_calls.append((record["source_id"], verified))
            or {"state": "no_newer_edition_listed"}))
    pinned = request("/api/update-check", dict(payload, source_id=SID))
    assert pinned["state"] == "no_newer_edition_listed"
    assert pinned_calls == [(SID, True)]
    assert not called
    result = request("/api/update-check", payload)
    assert result["state"] == "unable_to_determine"
    assert result["source_id"] == "update-fixture"
    assert called == ["update-fixture"]


@pytest.mark.parametrize("format_name", ["pdf", "epub"])
def test_document_update_check_never_starts_download(app, monkeypatch, format_name):
    request, _ = app
    catalog = request("/api/sources")
    sid = "document-update"
    catalog = mutate(request, catalog, "add",
                     {"title": "Document", "format": format_name,
                      "source_url": "https://publisher.org/manual." + format_name},
                     sid=sid)
    calls = []
    def check(record):
        calls.append(record["source_id"])
        return {"state": "representation_changed",
                "evidence": "changed strong ETag; newer edition unproven"}
    monkeypatch.setattr(admin_api.document_update_discovery, "check_installed_document", check)
    payload = {"source_id": sid, "revision": catalog["revision"]}
    assert request("/api/update-check", payload, status=403, origin="")["error"]
    assert request("/api/update-check", dict(payload, revision=0), status=409)["error"]
    assert not calls
    result = request("/api/update-check", payload)
    assert result["state"] == "representation_changed"
    assert result["source_id"] == sid
    assert calls == [sid]


@pytest.mark.parametrize("action,method", [
    ("zim-replacement-activate", "activate_candidate"),
    ("zim-replacement-rollback", "rollback_candidate"),
])
def test_zim_switch_requires_confirmation_and_schedules_reader_reconcile(
        app, monkeypatch, action, method):
    request, _ = app
    revision = request("/api/sources")["revision"]
    calls, refreshes = [], []

    def switch(sid, configuration):
        calls.append((sid, configuration["format"]))
        return {"accepted": True, "state": "completed"}

    monkeypatch.setattr(getattr(admin_api, "zim_jobs"), method, switch)
    monkeypatch.setattr(admin_api.zim_refresh, "schedule",
                        lambda load_records, **options: refreshes.append((load_records, options)))
    request_body = dict(source_id="fema-ready", action=action, revision=revision)
    assert request("/api/action", request_body, status=400)["error"]
    assert request("/api/action", dict(request_body, confirm_restart=False),
                   status=400)["error"]
    assert request("/api/action", dict(request_body, confirm_restart=True, extra="bad"),
                   status=400)["error"]
    assert not calls and not refreshes
    accepted = request("/api/action", dict(request_body, confirm_restart=True), status=202)
    assert accepted["accepted"] is True
    assert calls == [("fema-ready", "zim")]
    assert len(refreshes) == 1
    assert callable(refreshes[0][0])
    if action == "zim-replacement-activate":
        assert refreshes[0][1]["activated_source"] == "fema-ready"
        assert refreshes[0][1]["activated_configuration"]["format"] == "zim"
    else:
        assert refreshes[0][1] == {}


def test_zim_delete_is_explicit_confirmed_and_unpublished_at_http_boundary(
        app, monkeypatch):
    request, _ = app
    revision = request("/api/sources")["revision"]
    deletes = []
    monkeypatch.setattr(admin_api.zim_jobs, "delete_installed",
                        lambda sid: (deletes.append(sid) or
                                     {"accepted": True, "source_id": sid, "state": "deleted"}))
    payload = dict(source_id="fema-ready", action="zim-delete-installed", revision=revision)
    assert request("/api/action", payload, status=400)["error"]
    assert request("/api/action", dict(payload, confirm_restart=False), status=400)["error"]
    assert deletes == []

    publication.save("fema-ready", True)
    rejected = request("/api/action", dict(payload, confirm_restart=True), status=409)
    assert "Unpublish" in rejected["error"]
    assert deletes == []
    publication.save("fema-ready", False)
    result = request("/api/action", dict(payload, confirm_restart=True), status=202)
    assert result["state"] == "deleted"
    assert deletes == ["fema-ready"]


@pytest.mark.parametrize("action,method", [
    ("zim-replacement-prepare", "prepare_candidate"),
    ("zim-replacement-download", "start_candidate_acquisition"),
    ("zim-replacement-cancel", "cancel_candidate_acquisition"),
    ("zim-replacement-discard", "discard_candidate"),
])
def test_all_non_destructive_zim_replacement_actions_reach_guarded_http_handler(
        app, monkeypatch, action, method):
    request, _ = app
    revision = request("/api/sources")["revision"]
    calls = []

    def guarded_operation(sid, config):
        calls.append((sid, config["format"]))
        return {"accepted": True, "state": "prepared"}

    monkeypatch.setattr(admin_api.zim_jobs, method, guarded_operation)
    result = request("/api/action", dict(
        source_id="fema-ready", action=action, revision=revision), status=202)
    assert result["accepted"] is True
    assert calls == [("fema-ready", "zim")]
    assert request("/api/action", dict(
        source_id="fema-ready", action=action, revision=revision,
        extra=True), status=400)["error"]
    assert calls == [("fema-ready", "zim")]


def test_zim_controls_follow_capabilities_not_source_identity(app):
    request, _ = app
    catalog = request("/api/sources")
    entries = {source["source_id"]: source for source in catalog["sources"]}
    assert entries["wikipedia-en"]["legacy_pinned"] is True
    assert entries["wikipedia-en"].get("zim_download") is not True
    assert entries["fema-ready"]["legacy_pinned"] is False
    assert entries["fema-ready"]["zim_download"] is True
    template = admin_api.ADMIN_TEMPLATE_DIR / "admin-page.html.j2"
    assert "source.source_id == 'wikipedia-en'" not in template.read_text()


def test_pinned_wikipedia_shows_metadata_check_without_inert_download_actions(app):
    request, _ = app
    html = card(request("/admin/sources"), "wikipedia-en")
    assert html is not None
    assert "Check for updates" in html
    assert "data-zim-update-check" in html
    assert 'data-action="download"' not in html
    assert 'data-action="redownload"' not in html
    assert 'data-action="update"' not in html
    assert "data-zim-download" not in html


def test_verified_pinned_wikipedia_update_check_dispatches_read_only(app, monkeypatch):
    request, snapshot = app
    catalog = request("/api/sources")
    calls = []

    def fake_check(record, artifact, *, verified):
        calls.append((record["source_id"], artifact["sha256"], verified))
        return {"state": "no_newer_edition_listed", "installed_edition": "2026-08"}

    monkeypatch.setattr(admin_api.kiwix_update_discovery,
                        "refresh_pinned_seed", fake_check)
    payload = dict(source_id="wikipedia-en", revision=catalog["revision"])
    assert request("/api/update-check", payload)["state"] == "no_newer_edition_listed"
    assert calls == [("wikipedia-en", PIN["sha256"], True)]
    snapshot["acquisitions"] = []
    result = request("/api/update-check", payload)
    assert result["state"] == "no_newer_edition_listed"
    assert calls[-1] == ("wikipedia-en", PIN["sha256"], False)
    # Verified gating belongs in the real checker, not in HTTP's trust of
    # mutable client-provided metadata.


def test_fema_and_wikipedia_have_consistent_update_check_controls(app):
    request, _ = app
    page = request("/admin/sources")
    for sid in ("fema-ready", "wikipedia-en"):
        element = card(page, sid)
        assert element is not None
        assert "Check for updates" in element
        assert 'data-zim-update-check' in element


def test_update_discovery_configuration_and_endpoint_diagnostics(app, monkeypatch):
    request, _ = app
    cfg = admin_api.update_discovery_config
    current = request("/api/sources")
    before = source(current)
    assert before["discovery"] == {"method": "kiwix", "endpoint": cfg.OPDS, "periodic": True}
    saved = mutate(request, current, "edit", {"update_periodic": "no"})
    after = source(request("/api/sources"))
    assert not after["discovery"]["periodic"]
    for field in catalog_store.ARTIFACT_CONFIG_FIELDS:
        assert after["configuration"].get(field) == before["configuration"].get(field)
    assert after["publication"] == before["publication"]
    mutate(request, saved, "edit", {"update_endpoint": "https://publisher.org/catalog"}, status=400)

    probe = Mock(return_value={"reachable": True, "etag": '"private-value"'})
    monkeypatch.setattr(cfg.pdf_transport, "probe_document_metadata", probe)
    monkeypatch.setattr(admin_api, "ENDPOINT_TEST_NEXT", 0.0)
    payload = {"format": "pdf", "update_method": "head",
               "update_endpoint": "https://publisher.org/metadata"}
    for url in ("http://publisher.org/metadata", "https://127.0.0.1/path",
                "https://user:password@publisher.org/path", "https://publisher.org/path?secret=1"):
        request("/api/update-endpoint-test", dict(payload, update_endpoint=url), status=400)
    request("/api/update-endpoint-test", payload, origin="", status=403)
    probe.assert_not_called()
    result = request("/api/update-endpoint-test", payload)
    assert result["state"] == "reachable"
    assert result["checked_at"]
    assert "private-value" not in str(result)
    request("/api/update-endpoint-test", payload, status=429)
    probe.assert_called_once_with(payload["update_endpoint"])
    assert request("/api/sources")["revision"] == saved["revision"]


def test_public_search_groups_two_published_zims_and_hides_unpublished(app, monkeypatch):
    from theaxiom import search, zim_catalog
    request, _ = app
    legacy = publication.ROUTES[SID]["book_id"]
    second = "second-book." + "a" * 64
    publication.save(SID, True)
    publication.save("second-book", True)
    monkeypatch.setattr(admin_api.AdminAPIHandler, "_public_library", lambda self: ([
        {"source_id": SID, "name": "Encyclopedia", "category": "Kiwix"},
        {"source_id": "second-book", "name": "Emergency reference", "category": "Kiwix"},
    ], []))

    def acknowledged(records):
        enabled = publication.load()
        return {book: sid for book, sid in ((legacy, SID), (second, "second-book"))
                if enabled.get(sid)}

    def adapter(route, query):
        assert query == "water"
        return [search.Result(route["name"] + " article", route["path"] + "water")]

    monkeypatch.setattr(zim_catalog, "acknowledged_reader_books", acknowledged)
    monkeypatch.setattr(search, "KIWIX_ADAPTER", search.Adapter(search.KIWIX_CAPABILITY, adapter))
    page = request("/search?q=water")
    assert f'href="/zim/content/{legacy}/water"' in page
    assert f'href="/zim/content/{second}/water"' in page
    publication.save("second-book", False)
    assert "Emergency reference article" not in request("/search?q=water")
