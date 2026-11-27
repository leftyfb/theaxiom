"""Source profile regression tests through the real HTTP handler and template."""
import http.client
import json
import re
import threading
from unittest.mock import Mock

import pytest

from theaxiom import admin_api, catalog_store, publication, source_catalog, storage_settings
from test_admin_page_template import Parser
from test_zim_source import fixture_bytes
from test_zim_source import disk as disk


SID = "wikipedia-en"
PIN = source_catalog.WIKIPEDIA_SOURCE


@pytest.fixture
def app(tmp_path, monkeypatch):
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    monkeypatch.setattr(admin_api, "SOURCE_CATALOG", store)
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    monkeypatch.setattr(publication, "reader_location", lambda route, book_id=None: (route["path"] + "viewer#fixture/"))
    filename = f"{SID}.{PIN['sha256']}.zim"
    config = next(record["configuration"] for record in store.get()["sources"]
                  if record["source_id"] == SID)
    artifact = dict(filename=filename, source_url=PIN["source_url"], bytes=PIN["bytes"], sha256=PIN["sha256"])
    job = dict(source_id=SID, state="completed", verified=True, active=False,
               configuration=admin_api.zim_jobs.identity(config), artifact=artifact)
    original_status = admin_api.zim_jobs.status
    original_installed = admin_api.zim_jobs.installed
    monkeypatch.setattr(admin_api.zim_jobs, "status",
                        lambda sid: job if sid == SID else original_status(sid))
    monkeypatch.setattr(admin_api.zim_jobs, "installed",
                        lambda current, config: artifact if current is job and
                        admin_api.zim_jobs.matches(job, config) else original_installed(current, config))
    monkeypatch.setattr(admin_api.zim_catalog, "active_manifest_matches", lambda _, **kwargs: True)
    monkeypatch.setattr(admin_api.zim_catalog, "acknowledged_reader_books",
                        lambda _: {filename[:-4]: SID} if publication.load().get(SID) else {})
    snapshot = {"stale": False, "acquisitions": [{
        "source_id": SID, "state": "completed", "active": False, "verified": True,
        "filename": PIN["filename"], "expected_bytes": PIN["bytes"], "sha256": PIN["sha256"],
    }]}
    with admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler) as server:
        server.acquisition_monitor = Mock()
        server.acquisition_monitor.snapshot.return_value = snapshot
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, payload=None, status=200, *, raw=False, origin=None, upload_headers=None, get_headers=None):
            host, port = server.server_address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                if payload is None:
                    conn.request("GET", path, headers=get_headers or {})
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
    return next((item for item in re.findall(r'<details class="source-card"[^>]*>.*?</details></div>', page, re.S)
                 if f'data-source-id="{sid}"' in item), None)


def control(html, key, value):
    parser = Parser()
    parser.feed(html)
    return next(attrs for _, attrs in parser.tags if attrs.get(key) == value)


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
def test_active_and_unresolved_states_block_manual_changes(app, state, active):
    request, snapshot = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {"title": "Manual"}, sid="manual")
    for sid in ("manual",):
        snapshot["acquisitions"].append(dict(source_id=sid, state=state, active=active))
        rendered = card(request("/admin/sources"), sid)
        assert source(request("/api/sources"), sid)["mutation_blocked"]
        for action in ("edit", "remove"):
            assert "disabled" in control(rendered, f"data-catalog-{action}", sid)
            mutate(request, catalog, action, {"title": "Changed"} if action == "edit" else {},
                   sid=sid, status=400)
    assert "Use the ZIM Download action" in refuse_action(request, catalog, 409)


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
    assert not publication.load().get(SID, False)
    publish(request, True)
    removed = mutate(request, request("/api/sources"), "remove")
    assert not any(row["source_id"] == SID for row in removed["sources"])
    assert not publication.load().get(SID, False)
    assert request("/read/zim/wikipedia-en", status=403)


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
    assert 'href="/read/zim/wikipedia-en"' in page
    assert 'Private fixture' not in page and 'value="private"' not in page
    assert 'value="reference" selected' in page
    assert 'href="/read/zim/wikipedia-en"' not in request("/?collection=private")
    assert "No published sources match" in request("/?category=missing")
    request("/?category=reference&category=other", status=400)
    request("/?tag=../bad", status=400)
    search_page = request("/search?q=water&tag=mixed")
    assert "No matching results." not in search_page
    assert "Search incomplete" in search_page
    assert '<h2 id="source-wikipedia-en">' not in search_page
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
    assert source(request("/api/sources"))["download_filter"] == "downloaded"


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
    monkeypatch.setattr(admin_api.zim_catalog, "active_manifest_matches", lambda _, **kwargs: False)
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
    assert updated["revision"] == result["revision"]
    assert updated["library_order"][-1] == sid
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
    assert entries["wikipedia-en"]["zim_download"] is True
    assert entries["fema-ready"]["zim_download"] is True
    template = admin_api.ADMIN_TEMPLATE_DIR / "admin-page.html.j2"
    assert "source.source_id == 'wikipedia-en'" not in template.read_text()


def test_readding_wikipedia_uses_the_generic_zim_lifecycle(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    fields = source(catalog)["profile"] | source(catalog)["configuration"]
    catalog = mutate(request, catalog, "remove")
    assert card(request("/admin/sources")) is None

    with monkeypatch.context() as patch:
        patch.setattr(admin_api.zim_jobs, "status", lambda _: None)
        catalog = mutate(request, catalog, "add", fields)
        added = source(catalog)
        assert added["zim_download"]
        assert not added["installed_verified"]
        assert not added["publication"]["enabled"]
        publish(request, True, status=409)
        request("/read/zim/wikipedia-en", status=403)

    publish(request, True)
    assert request("/read/zim/wikipedia-en", status=302) == "/zim/viewer#fixture/"
    assert 'href="/read/zim/wikipedia-en"' in request("/")
    assert 'href="/read/wikipedia-en"' not in request("/")


def test_wikipedia_update_check_dispatches_to_managed_zim_checker(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    calls = []

    def check(source_id):
        calls.append(source_id)
        return {"state": "no_newer_edition_listed"}

    monkeypatch.setattr(admin_api.kiwix_update_discovery, "refresh_checked_source", check)
    payload = dict(source_id="wikipedia-en", revision=catalog["revision"])
    assert request("/api/update-check", payload)["state"] == "no_newer_edition_listed"
    assert calls == ["wikipedia-en"]


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


def test_upstream_catalog_refresh_uses_only_filtered_metadata(app, monkeypatch):
    request, _ = app
    module = admin_api.upstream_catalog
    refresh = Mock(return_value={"entries": [], "cached": True, "checked_at": 1000})
    monkeypatch.setattr(module, "refresh", refresh)
    before = request("/api/sources")["revision"]
    payload = {"family": "wikipedia_en_all", "language": "eng"}
    request("/api/upstream-kiwix-catalog", payload, origin="", status=403)
    request("/api/upstream-kiwix-catalog", payload | {"url": "https://unknown.example/"}, status=400)
    refresh.assert_not_called()
    assert request("/api/upstream-kiwix-catalog", payload)["cached"]
    refresh.assert_called_once_with(family="wikipedia_en_all", language="eng")
    assert request("/api/sources")["revision"] == before


def test_portable_selection_round_trip_discloses_only_selected_public_metadata(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {
        "title": "Selection fixture", "format": "zim", "language": "en", "edition": "2026-11",
        "source_url": "https://example.org/library.zim", "license": "Fixture terms",
        "expected_size_bytes": "400",
    }, sid="selection-fixture")
    endpoint = "/api/library-selection"
    payload = {"revision": catalog["revision"], "source_ids": ["selection-fixture"]}
    request(endpoint, payload, status=403, origin="")
    request(endpoint, payload | {"revision": 0}, status=409)
    exported = request(endpoint, payload)["manifest"]
    assert exported == {"version": 1, "sources": [{
        "source_id": "selection-fixture", "format": "zim", "language": "en", "edition": "2026-11",
    }]}
    settings = storage_settings.DEFAULT_SETTINGS | {"minimum_free_bytes": 100}
    monkeypatch.setattr(storage_settings, "load_settings", lambda: settings)
    probe = Mock(return_value={"write_ready": True, "free_bytes": 500})
    monkeypatch.setattr(admin_api.STORAGE_PROBE, "status", probe)
    jobs = admin_api.zim_jobs
    start = Mock(side_effect=AssertionError("selection preview must never start an acquisition"))
    monkeypatch.setattr(jobs, "start", start)
    preview = request(endpoint, {"manifest": exported})
    assert preview["revision"] == catalog["revision"]
    assert preview["sources"] == [{
        "source_id": "selection-fixture", "state": "match", "title": "Selection fixture",
        "format": "zim", "language": "en", "edition": "2026-11", "license": "Fixture terms",
    }]
    assert preview["storage"] == {
        "sources": [{"source_id": "selection-fixture", "title": "Selection fixture", "state": "estimated", "format": "zim",
                     "transfer_bytes": 400, "installed_bytes": 0, "staging_bytes": 400, "peak_bytes": 400}],
        "capacity": {"state": "fits", "reserve_bytes": 100, "free_bytes": 500, "available_bytes": 400},
    }
    replacement = {
        "state": "failed",
        "configuration": jobs.identity({"format": "zim", "source_url": "https://example.org/other.zim",
                                        "expected_size_bytes": "100"}),
    }
    monkeypatch.setattr(jobs, "read_candidate", lambda sid: replacement)
    estimate = request(endpoint, {"manifest": exported})["storage"]
    assert estimate["sources"][0]["state"] == "review_replacement"
    assert "transfer_bytes" not in estimate["sources"][0]
    assert estimate["capacity"]["state"] == "uncertain"
    entry = exported["sources"][0]
    for changes, state in (({"edition": "2026-10"}, "different"), ({"source_id": "missing"}, "missing")):
        manifest = exported | {"sources": [entry | changes]}
        probe.reset_mock()
        estimate = request(endpoint, {"manifest": manifest})
        row = estimate["sources"][0]
        assert row["state"] == state
        assert all(row[key] == value for key, value in (entry | changes).items())
        assert row.get("local_choice") == (entry if state == "different" else None)
        assert estimate["storage"] == {"sources": [], "capacity": {"state": "not_applicable"}}
        probe.assert_not_called()
    for invalid in (exported | {"version": 2}, exported | {"sources": [entry, entry]},
                    exported | {"sources": [entry | {"source_url": "https://unknown.example/"}]}):
        request(endpoint, {"manifest": invalid}, status=400)
    assert request("/api/sources")["revision"] == catalog["revision"]
    start.assert_not_called()


def test_export_local_transfer_manifest_checks_verified_zim_without_copying(app, tmp_path, monkeypatch):
    import hashlib

    request, _ = app
    sid = "transfer-fixture"
    url = "https://example.org/library.zim"
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "add", {
        "title": "Portable reference", "format": "zim", "language": "eng", "edition": "2026-01",
        "source_url": url, "license": "Test license", "provenance": "Test publisher record",
    }, sid=sid)
    library = tmp_path / "library" / "zim"
    library.mkdir(parents=True)
    mount = admin_api.storage._covering(tmp_path, admin_api.storage.read_mounts())
    monkeypatch.setattr(admin_api.pdf_source, "LOCAL_FILESYSTEMS", {mount["type"]})
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(tmp_path)))
    contents = b"synthetic transfer fixture"
    digest = hashlib.sha256(contents).hexdigest()
    filename = f"{sid}.{digest}.zim"
    archive = library / filename
    archive.write_bytes(contents)
    archive.chmod(0o600)
    jobs = admin_api.zim_jobs
    config = source(catalog, sid)["configuration"]
    job = dict(source_id=sid, state="completed", configuration=jobs.identity(config), verified=True,
               artifact=dict(filename=filename, sha256=digest, bytes=len(contents), source_url=url))
    jobs.save(job)
    route = "/api/library-selection"
    payload = {"revision": catalog["revision"], "source_ids": [sid], "transfer_manifest": True}
    request(route, payload, origin="", status=403)
    request(route, payload | {"revision": 0}, status=409)
    request(route, payload | {"source_ids": [sid, sid]}, status=400)
    request(route, payload | {"transfer_manifest": "true"}, status=400)
    before = archive.stat()
    manifest = request(route, payload)["manifest"]
    assert manifest == {
        "version": 1, "kind": "axiom-local-transfer", "artifacts": [{
            "source_id": sid, "format": "zim", "language": "eng", "edition": "2026-01",
            "title": "Portable reference", "filename": filename, "bytes": len(contents),
            "sha256": digest, "license": "Test license", "provenance": "Test publisher record",
        }],
    }
    assert archive.stat().st_mtime_ns == before.st_mtime_ns
    assert jobs.read(sid) == job
    assert request("/api/sources")["revision"] == catalog["revision"]
    jobs.save(job | {"verified": False})
    assert "Verified installation unavailable" in request(route, payload, status=400)["error"]
    jobs.save(job)
    separate = library / "separate.zim"
    archive.rename(separate)
    archive.symlink_to(separate)
    assert "Verified installation unavailable" in request(route, payload, status=400)["error"]

    # The preflight sees the ledger but does not open the archive, even if
    # the local path has since been replaced.
    monkeypatch.setattr(jobs, "installed", Mock(side_effect=AssertionError("preflight must not open archives")))
    item = manifest["artifacts"][0]
    for changes, expected in (({}, "matching_record"),
                              ({"sha256": "b" * 64}, "conflicting_record"),
                              ({"source_id": "other-source"}, "new_source")):
        result = request(route, {"transfer_preview": manifest | {"artifacts": [item | changes]}})
        assert result["transfer_artifacts"][0]["state"] == expected
    for filename in ("../secret.zim", "/var/archive.zim", ".secret.zim", "part..backup.zim"):
        request(route, {"transfer_preview": manifest | {"artifacts": [item | {"filename": filename}]}}, status=400)
    request(route, {"transfer_preview": manifest | {"artifacts": [item, item]}}, status=400)
    request(route, {"transfer_preview": manifest | {"artifacts": [item, item | {"source_id": "second"}]}}, status=400)
    request(route, {"transfer_preview": manifest | {"version": True}}, status=400)
    assert admin_api.SOURCE_CATALOG.get()["revision"] == catalog["revision"]
    assert jobs.read(sid) == job


def test_selection_presets_report_missing_planned_and_bounded_configured_sources(app):
    request, _ = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "remove", sid="noaa")
    catalog = mutate(request, catalog, "add", {
        "title": "Compact guide", "format": "pdf", "expected_size_bytes": "1024",
        "source_url": "https://example.org/compact.pdf", "license": "Fixture terms",
    }, sid="compact-guide")
    route = "/api/library-selection"
    request(route, {"preset": "emergency"}, origin="", status=403)
    request(route, {"preset": ["medical"]}, status=400)
    request(route, {"preset": "unlisted"}, status=400)
    emergency = request(route, {"preset": "emergency"})
    assert emergency["revision"] == catalog["revision"]
    assert [(item["source_id"], item["state"]) for item in emergency["sources"]] == [
        ("fema-ready", "match"), ("epa-water", "match"), ("noaa", "missing"),
        ("amateur-radio", "match"),
    ]
    assert [row["source_id"] for row in emergency["storage"]["sources"]] == ["fema-ready", "epa-water", "amateur-radio"]
    lightweight = request(route, {"preset": "lightweight"})
    assert [row["source_id"] for row in lightweight["sources"]] == ["epa-water", "cdc", "usda-preservation", "usda-extension", "practical-action", "amateur-radio", "compact-guide"]
    assert [row["transfer_bytes"] for row in lightweight["storage"]["sources"]] == [754723, 282412, 3794234, 223610, 652708, 355690, 1024]
    assert request("/api/sources")["revision"] == catalog["revision"]


def test_acquisition_preview_uses_recorded_sizes_without_starting_jobs(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    definitions = [
        ("replace-me", {"format": "zim", "source_url": "https://example.org/new.zim", "expected_size_bytes": "400"}),
        ("unknown-size", {"format": "zim", "source_url": "https://example.org/unknown.zim"}),
        ("document", {"format": "pdf", "source_url": "https://example.org/file.pdf", "expected_size_bytes": "900"}),
        ("unknown-epub", {"format": "epub", "source_url": "https://example.org/book.epub"}),
        ("upload-epub", {"format": "epub"}),
        ("oversized-pdf", {"format": "pdf", "source_url": "https://example.org/large.pdf", "expected_size_bytes": "99999999"}),
        ("unusable-url", {"format": "pdf", "source_url": "http://example.org/plain.pdf"}),
    ]
    for sid, fields in definitions:
        catalog = mutate(request, catalog, "add", dict(fields, title=sid), sid=sid)
    jobs = admin_api.zim_jobs
    old = jobs.identity({"format": "zim", "source_url": "https://example.org/old.zim"})
    installed = dict(source_id="replace-me", state="completed", configuration=old, verified=True,
                     artifact={"bytes": 200})
    jobs.save(installed)
    forbidden = Mock(side_effect=AssertionError("preview must not start or reconcile acquisition"))
    monkeypatch.setattr(jobs, "status", forbidden)
    monkeypatch.setattr(jobs, "start", forbidden)
    monkeypatch.setattr(jobs, "installed", forbidden)
    settings = storage_settings.DEFAULT_SETTINGS | {"minimum_free_bytes": 100}
    monkeypatch.setattr(storage_settings, "load_settings", lambda: settings)
    probe = Mock(return_value={"write_ready": True, "free_bytes": 500})
    monkeypatch.setattr(admin_api.STORAGE_PROBE, "status", probe)
    payload = {"revision": catalog["revision"], "source_ids": [sid for sid, _ in definitions]}
    request("/api/acquisition-preview", payload, status=403, origin="")
    request("/api/acquisition-preview", payload | {"revision": 0}, status=409)
    request("/api/acquisition-preview", payload | {"source_ids": ["replace-me", "replace-me"]}, status=400)
    request("/api/acquisition-preview", payload | {"source_ids": ["missing"]}, status=400)
    result = request("/api/acquisition-preview", payload)
    planned, unknown, document, unknown_epub, upload, oversized, invalid = result["sources"]
    assert planned == {"state": "estimated", "source_id": "replace-me", "title": "replace-me", "format": "zim",
                       "installed_bytes": 200, "transfer_bytes": 400, "staging_bytes": 400,
                       "peak_bytes": 600}
    assert unknown["transfer_bytes"] is None and unknown["peak_bytes"] is None
    assert document == {"source_id": "document", "title": "document", "format": "pdf", "state": "estimated",
                        "transfer_bytes": 900, "staging_bytes": 900, "installed_bytes": None, "peak_bytes": None}
    assert unknown_epub["format"] == "epub" and unknown_epub["staging_bytes"] is None
    assert [upload["state"], oversized["state"], invalid["state"]] == [
        "upload_required", "over_size_limit", "invalid_download_url"]
    probe.assert_called_once_with(settings, formats=("epub", "pdf", "zim"))
    assert result["capacity"] == {"state": "insufficient", "free_bytes": 500, "reserve_bytes": 100,
                                  "available_bytes": 400}
    probe.reset_mock()
    documents_only = payload | {"source_ids": ["document", "unknown-epub"]}
    probe.return_value["free_bytes"] = 1000
    bounded = 900 + admin_api.epub_source.MAX_BYTES
    capacity = request("/api/acquisition-preview", documents_only)["capacity"]
    assert capacity["state"] == "uncertain"
    assert capacity["staging_upper_bound_bytes"] == bounded
    probe.assert_called_once_with(settings, formats=("epub", "pdf"))
    probe.return_value["free_bytes"] = bounded + settings["minimum_free_bytes"]
    capacity = request("/api/acquisition-preview", documents_only)["capacity"]
    assert capacity["state"] == "fits_with_unknown_sizes"
    assert capacity["staging_upper_bound_bytes"] == bounded
    mixed = request("/api/acquisition-preview", payload)["capacity"]
    assert mixed["state"] == "uncertain"
    assert "staging_upper_bound_bytes" not in mixed
    probe.reset_mock()
    upload_only = payload | {"source_ids": ["upload-epub"]}
    assert request("/api/acquisition-preview", upload_only)["capacity"] == {"state": "not_applicable"}
    probe.assert_not_called()
    sized = payload | {"source_ids": ["replace-me"]}
    assert request("/api/acquisition-preview", sized)["capacity"]["state"] == "fits"
    probe.return_value["free_bytes"] = 499
    assert request("/api/acquisition-preview", payload)["capacity"]["state"] == "insufficient"
    probe.return_value = {"pending": True}
    assert request("/api/acquisition-preview", sized)["capacity"] == {"state": "pending"}
    probe.return_value = {"error": "required_mount_missing_or_replaced", "free_bytes": 5000}
    assert request("/api/acquisition-preview", sized)["capacity"] == {"state": "unavailable"}
    assert jobs.read("replace-me") == installed
    assert admin_api.SOURCE_CATALOG.get()["revision"] == catalog["revision"]
    forbidden.assert_not_called()


def test_public_layout_order_groups_reset_and_publication_boundary(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    for sid, title in (("later", "Later reference"), ("private", "Private reference")):
        catalog = mutate(request, catalog, "add", {"title": title, "format": "zim"}, sid=sid)
    for sid in (SID, "fema-ready", "later"):
        publication.save(sid, True)

    def zim_status(record, records, **kwargs):
        sid = record["source_id"]
        enabled = publication.load().get(sid, False)
        return {"published": enabled, "verified": enabled, "entry_path": "/read/zim/" + sid}

    monkeypatch.setattr(admin_api.zim_catalog, "status", zim_status)
    original = request("/")
    first = 'href="/read/zim/wikipedia-en"'
    second = 'href="/read/zim/fema-ready"'
    later = 'href="/read/zim/later"'
    assert original.index(first) < original.index(second) < original.index(later)
    group_id = "science-education"
    catalog = request("/api/groups", {"operation": "add-members", "kind": "categories",
                      "group_id": group_id, "source_ids": [SID, "fema-ready", "private"],
                      "revision": catalog["revision"]})
    order = ["fema-ready", "private", SID]
    layout = {"source_order": order, "group_by": "categories"}
    payload = {"revision": catalog["revision"], "layout": layout}
    request("/api/library-layout", payload, status=403, origin="")
    request("/api/library-layout", payload | {"layout": layout | {"source_order": ["unknown"]}}, status=400)
    request("/api/library-layout", payload | {"layout": layout | {"source_order": [SID, SID]}}, status=400)
    saved = request("/api/library-layout", payload)
    assert saved["layout"] == layout
    request("/api/library-layout", payload, status=409)
    page = request("/")
    assert page.index(second) < page.index(first) < page.index(later)
    assert "<h2>Science &amp; Education</h2>" in page
    assert "<h2>Other sources</h2>" in page
    assert page.index('id="library-search"') < page.index(second)
    assert page.index('href="#getting-started"') < page.index(second)
    assert "Private reference" not in page
    filtered = request("/?category=" + group_id)
    assert second in filtered and first in filtered and later not in filtered
    assert "<h2>Science &amp; Education</h2>" not in filtered
    assert "Private reference" not in filtered
    admin_order = request("/api/sources")["library_order"]
    assert admin_order.index("fema-ready") < admin_order.index(SID) < admin_order.index("later")
    saved = request("/api/library-layout", {
        "revision": saved["revision"], "layout": {
            "source_order": ["fema-ready", "later", SID, "private"],
            "group_by": "categories", "preserve_source_order": True,
        },
    })
    reordered = request("/")
    assert reordered.index(second) < reordered.index(later) < reordered.index(first)
    admin_order = request("/api/sources")["library_order"]
    assert admin_order[:3] == ["fema-ready", "later", SID]
    removed = mutate(request, saved, "remove", sid="private")
    assert removed["library_layout"]["source_order"] == ["fema-ready", "later", SID]
    assert request("/api/library-layout", {"revision": removed["revision"], "layout": None})["layout"] is None
    reset_page = request("/")
    assert reset_page.index(first) < reset_page.index(second) < reset_page.index(later)
    assert "<h2>Science &amp; Education</h2>" not in reset_page


def test_public_search_groups_two_published_zims_and_hides_unpublished(app, monkeypatch):
    from theaxiom import search, zim_catalog
    request, _ = app
    legacy = f"{SID}.{PIN['sha256']}"
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
    assert f'href="/zim/viewer#{legacy}/water"' in page
    assert f'href="/zim/viewer#{second}/water"' in page
    progressive = request(f"/search?q=water&mode=source&source={SID}")
    assert progressive["results"][0]["href"] == f"/zim/viewer#{legacy}/water"
    publication.save("second-book", False)
    assert "Emergency reference article" not in request("/search?q=water")


def test_storage_preferences_block_unresolved_acquisition(app, monkeypatch, tmp_path):
    request, snapshot = app
    path = tmp_path / "storage.json"
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", path)
    saved = dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(tmp_path / "old"))
    storage_settings.save_settings(saved)
    changed = dict(saved, storage_root=str(tmp_path / "new"))
    snapshot["acquisitions"][0].update(active=True, state="downloading")
    assert "resolve the acquisition" in request("/api/storage-settings", changed, status=409)["error"]
    assert storage_settings.load_settings() == saved
    assert request("/api/storage-settings", saved) == saved
    snapshot["acquisitions"][0].update(active=False, state="completed")
    monkeypatch.setattr(admin_api.zim_jobs, "read", lambda sid: {"state": "paused"})
    assert "resolve the acquisition" in request("/api/storage-settings", changed, status=409)["error"]
    monkeypatch.setattr(admin_api.zim_jobs, "read", lambda sid: None)
    assert request("/api/storage-settings", changed) == changed


def test_public_search_paginates_only_selected_published_source(app, monkeypatch):
    from html import unescape

    request, _ = app
    handler = admin_api.AdminAPIHandler
    sources = [{"source_id": sid, "name": sid} for sid in ("reference", "guide")]
    groups = [dict(kind="tags", group_id="safety", name="Safety",
                   source_ids=["reference", "guide", "private"])]
    monkeypatch.setattr(handler, "_public_library", lambda self: (sources, groups))
    calls = []

    def search(self, query, eligible, *, page, start_base):
        calls.append((query, [source["source_id"] for source in eligible], page, start_base))
        return [
            dict(source_id=source["source_id"], name=source["name"], status="ok",
                 results=[admin_api.library_search.Result(f"Hit on page {page}", "/article")],
                 has_more=page == 1, page=page, start_base=0)
            for source in eligible
        ]

    monkeypatch.setattr(handler, "_search_published_content", search)
    first, headers = request("/search?q=food%26water&tag=safety", raw=True)
    assert headers["Cache-Control"] == "no-store"
    links = re.findall(r'rel="next" href="([^"]+)"', first.decode())
    assert len(links) == 2
    second = request(unescape(links[0]))
    assert "Hit on page 2" in second
    assert 'start="21"' in second
    assert 'rel="prev"' in second and 'rel="next"' not in second
    assert calls == [("food&water", ["reference", "guide"], 1, 1),
                     ("food&water", ["reference"], 2, 0)]
    assert "No published collections" in request("/search?q=water&source=private&page=2")
    assert calls[-1][1] == []
    calls.clear()
    shell = request("/search?q=water&tag=safety&mode=progressive")
    assert 'data-source="reference"' in shell and 'data-source="guide"' in shell
    assert 'data-source="private"' not in shell and calls == []
    payload = request("/search?q=water&tag=safety&mode=source&source=guide&page=2&base=0")
    assert payload["results"][0]["title"] == "Hit on page 2"
    assert calls == [("water", ["guide"], 2, 0)]
    request("/search?q=water&tag=safety&mode=source&source=private", status=404)
    request("/search?q=water&mode=source", status=400)
    request("/search?q=water&page=2", status=400)
    request("/search?q=water&source=reference&page=0", status=400)
    request("/search?q=water&source=reference&page=2&base=3", status=400)


def test_acquisition_preview_adds_retained_document_bytes_to_peak(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    for sid, format_name, size in [("guide", "pdf", "900"), ("book", "epub", "")]:
        catalog = mutate(request, catalog, "add", dict(
            title=sid, format=format_name, expected_size_bytes=size,
            source_url=f"https://example.org/{sid}.{format_name}"), sid=sid)
    measured = Mock(side_effect=[{"guide": 120}, {"book": 240}])
    monkeypatch.setattr(admin_api.pdf_source, "preview_installed_sizes", measured)
    result = request("/api/acquisition-preview", {
        "revision": catalog["revision"], "source_ids": ["guide", "book"],
    })
    pdf, epub = result["sources"]
    assert pdf["installed_bytes"] == 120 and pdf["peak_bytes"] == 1020
    assert epub["installed_bytes"] == 240 and epub["peak_bytes"] is None
    assert [call.args for call in measured.call_args_list] == [
        (["guide"], "pdf"), (["book"], "epub"),
    ]


def test_confirmed_batch_reuses_document_acquisition_and_revalidates_capacity(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    for sid in ("first", "second"):
        catalog = mutate(request, catalog, "add", {
            "title": sid, "format": "pdf", "source_url": f"https://example.org/{sid}.pdf",
            "expected_size_bytes": "100",
        }, sid=sid)
    available = Mock(return_value=300)
    monkeypatch.setattr(admin_api, "_batch_available", available)
    acquire = Mock()
    monkeypatch.setattr(admin_api, "_acquire_document", acquire)
    payload = {"revision": catalog["revision"], "source_ids": ["first", "second"], "confirm": True}
    request("/api/acquisition-batch", payload, status=403, origin="")
    request("/api/acquisition-batch", payload | {"confirm": False}, status=400)
    batch = request("/api/acquisition-batch", payload, status=202)
    assert len(batch["job_ids"]) == 2
    request("/api/acquisition-batch", payload, status=409)
    acquire.assert_not_called()

    queue = admin_api.acquisition_queue
    shutdown = threading.Event()
    assert queue.run_next(admin_api._execute_batch_job, shutdown)
    assert acquire.call_args.args[0] == "first"
    available.return_value = 99
    assert queue.run_next(admin_api._execute_batch_job, shutdown)
    acquire.assert_called_once()
    jobs = request("/api/acquisition-batch")["jobs"]
    assert [(job["source_id"], job["state"]) for job in jobs] == [
        ("second", "failed"), ("first", "completed"),
    ]


@pytest.mark.parametrize("format_name", ["pdf", "epub"])
def test_document_batch_retries_inside_the_same_job(app, monkeypatch, format_name):
    from contextlib import contextmanager

    request, _ = app
    catalog = request("/api/sources")
    sid = "retry-fixture"
    url = f"https://example.org/fixture.{format_name}"
    catalog = mutate(request, catalog, "add", {
        "title": "Fixture", "format": format_name, "source_url": url,
        "expected_size_bytes": "100", "language": "en",
    }, sid=sid)
    transport = admin_api.pdf_transport
    progress = []
    attempts = []
    artifact = {"bytes": 100, "sha256": "a" * 64}

    def transfer():
        attempts.append(None)
        if len(attempts) == 1:
            raise transport.RetryableDownloadError("fixture HTTP 503", retry_after=3)

    if format_name == "pdf":
        def download(*args, **kwargs):
            transfer()
            return artifact, None
        monkeypatch.setattr(transport, "download", download)
    else:
        @contextmanager
        def open_fixture(*args, **kwargs):
            transfer()
            yield object()
        monkeypatch.setattr(transport, "open_pdf", open_fixture)
        monkeypatch.setattr(admin_api.epub_source, "install_stream", Mock(return_value=(artifact, {})))

    monkeypatch.setattr(admin_api, "_batch_available", Mock(return_value=300))
    batch = request("/api/acquisition-batch", {
        "revision": catalog["revision"], "source_ids": [sid], "confirm": True,
    }, status=202)

    def wait(delay, stopped):
        progress.append(delay)
        status = request("/api/acquisition-batch")
        assert [job["id"] for job in status["jobs"]] == batch["job_ids"]
        assert status["jobs"][0]["state"] == "running"
        assert status["document_retries"][sid] == {
            "state": "retrying", "automatic_retry_count": 1, "retry_delay_seconds": 3,
        }
        assert not stopped()

    monkeypatch.setattr(transport, "_wait_for_retry", wait)
    assert admin_api.acquisition_queue.run_next(admin_api._execute_batch_job, threading.Event())
    status = request("/api/acquisition-batch")
    assert [job["id"] for job in status["jobs"]] == batch["job_ids"]
    assert status["jobs"][0]["state"] == "completed"
    assert "document_retries" not in status
    assert source(request("/api/sources"), sid)["configuration"]["sha256"] == artifact["sha256"]
    assert len(attempts) == 2 and progress == [3]


def test_batch_restarts_zim_from_retained_bytes(app, monkeypatch):
    import json

    request, _ = app
    catalog = request("/api/sources")
    updated = mutate(request, catalog, "add", {
        "title": "Fixture", "format": "zim", "source_url": "https://example.org/data.zim",
        "expected_size_bytes": "1000",
    }, sid="fixture")
    config = source(updated, "fixture")["configuration"]
    jobs = admin_api.zim_jobs
    old = dict(state="paused", configuration=jobs.identity(config))
    monkeypatch.setattr(jobs, "status", Mock(side_effect=[old, old | {"state": "completed"}]))
    monkeypatch.setattr(jobs, "staged_size", Mock(return_value=300))
    start = Mock()
    monkeypatch.setattr(jobs, "start", start)
    monkeypatch.setattr(admin_api, "_batch_available", Mock(return_value=700))
    job = dict(source_id="fixture", operation="zim", state="running", bytes=1000,
               configuration=json.dumps(config),
               storage=json.dumps(admin_api.storage_settings.load_settings()))
    assert admin_api._execute_batch_job(job, lambda: False, threading.Event()) == ("completed", "")
    start.assert_called_once_with("fixture", config, resume=True, restart=False)


def test_existing_source_placeholders_are_configured_without_downloads(app, monkeypatch):
    import json

    request, _ = app
    catalog = request("/api/sources")
    store = admin_api.SOURCE_CATALOG
    state = json.loads(store.path.read_text())
    placeholders = {entry["source_id"]: entry for entry in source_catalog.planned_sources()}
    for index, record in enumerate(state["sources"]):
        sid = record["source_id"]
        if sid in {"epa-water", "cdc"}:
            state["sources"][index] = store._seed_record(placeholders[sid])
    state["sources"].append(store._record("operator-source", {"title": "Operator entry"}))
    store.path.write_text(json.dumps(state))
    fetch = Mock(side_effect=AssertionError("Catalog migration must not fetch content"))
    monkeypatch.setattr(admin_api.pdf_transport, "download", fetch)

    upgraded = request("/api/sources")
    assert upgraded["revision"] == catalog["revision"] + 1
    for sid, expected in source_catalog.CONFIGURED_PDFS.items():
        record = source(upgraded, sid)
        assert record["configuration"]["format"] == "pdf"
        assert record["configuration"]["source_url"] == expected["source_url"]
        assert record["configuration"]["sha256"] == expected["sha256"]
        assert not record["publication"]["published"]
    assert source(upgraded, "operator-source")["profile"]["title"] == "Operator entry"
    assert request("/api/sources")["revision"] == upgraded["revision"]
    assert request("/api/acquisition-batch") == {"jobs": []}
    assert 'id="starter-pdfs"' not in request("/admin/sources")
    fetch.assert_not_called()


def test_configured_defaults_fill_blank_download_settings_preserving_edits(app):
    import json

    request, _ = app
    catalog = request("/api/sources")
    store = admin_api.SOURCE_CATALOG
    state = json.loads(store.path.read_text())
    state["sources"] = [record for record in state["sources"]
                        if record["source_id"] not in {"epa-water", "cdc"}]
    entry = next(entry for entry in source_catalog.planned_sources()
                 if entry["source_id"] == "cdc")
    edited = store._seed_record(entry)
    edited["profile"]["title"] = "My custom CDC profile"
    edited["configuration"].update(format="pdf", source_url="", expected_size_bytes="", sha256="",
                                   provenance="My notes")
    state["sources"].append(edited)
    store.path.write_text(json.dumps(state))

    upgraded = request("/api/sources")
    assert upgraded["revision"] == catalog["revision"] + 1
    assert not any(row["source_id"] == "epa-water" for row in upgraded["sources"])
    cdc = source(upgraded, "cdc")
    assert cdc["configuration"]["format"] == "pdf"
    assert cdc["configuration"]["source_url"] == source_catalog.CONFIGURED_PDFS["cdc"]["source_url"]
    assert cdc["configuration"]["provenance"] == "My notes"
    assert source(upgraded, "cdc")["profile"]["title"] == "My custom CDC profile"


def test_configured_defaults_leave_custom_download_pin_intact(app):
    request, _ = app
    catalog = request("/api/sources")
    custom = {"format": "pdf", "source_url": "https://example.org/handwashing.pdf",
              "expected_size_bytes": "", "sha256": ""}
    saved = mutate(request, catalog, "edit", custom, sid="cdc")
    loaded = request("/api/sources")
    assert loaded["revision"] == saved["revision"]
    config = source(loaded, "cdc")["configuration"]
    assert all(config[key] == value for key, value in custom.items())


def test_update_schedule_and_library_overview(app, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    request, _ = app
    schedule = admin_api.update_schedule
    next_legacy = datetime(2026, 11, 27, 3, 22, tzinfo=ZoneInfo("UTC"))
    monkeypatch.setattr(schedule, "_legacy_next", lambda zone: next_legacy.astimezone(zone))
    legacy = request("/api/update-schedule")
    assert legacy["interval_days"] == 1 and legacy["start_time"] == "03:00"
    assert legacy["time_zone"] and legacy["next_run"]
    request("/api/update-schedule", {"interval_days": 0, "start_time": "25:00"}, status=400)
    saved = request("/api/update-schedule", {"interval_days": 3, "start_time": "06:45"})
    assert request("/api/update-schedule") == saved
    assert saved["interval_days"] == 3 and "T06:45:00" in saved["next_run"]
    assert "Update check schedule" in request("/admin/settings")

    publication.save(SID, True)
    overview = request("/api/library-overview")
    assert overview["total_sources"] == len(request("/api/sources")["sources"])
    assert overview["enabled_sources"] == 1
    assert overview["disabled_sources"] == overview["total_sources"] - 1
    assert overview["installed_sources"] == 1
    assert overview["installed_bytes"] == PIN["bytes"]
    assert overview["revision"] == request("/health")["revision"]


def test_manual_update_sweep_is_metadata_only_and_reports_busy(app, monkeypatch):
    request, _ = app
    scheduler = admin_api.kiwix_update_scheduler
    start = Mock(side_effect=[True, False])
    monkeypatch.setattr(scheduler, "start_manual", start)
    request("/api/update-sweep", {}, status=400)
    assert request("/api/update-sweep", {"confirm": True}, status=202) == {"state": "running"}
    request("/api/update-sweep", {"confirm": True}, status=409)
    assert request("/api/update-sweep") == {"state": "idle", "checks": []}


def test_batch_updates_keep_old_archives_and_isolate_download_and_activation_failures(app, disk, monkeypatch):
    from contextlib import contextmanager
    import hashlib
    from io import BytesIO

    request, _ = app
    catalog = request("/api/sources")
    old_data = fixture_bytes()
    new_body = old_data[:103] + b"x"
    new_data = new_body + hashlib.md5(new_body, usedforsecurity=False).digest()
    new_digest = hashlib.sha256(new_data).hexdigest()
    installed = {}
    updates = {}
    jobs, archives = admin_api.zim_jobs, admin_api.zim_source
    queue = admin_api.acquisition_queue

    for sid in ("bad-download", "bad-reader", "ready-public", "ready-private"):
        prefix = f"https://download.kiwix.org/zim/zimit/{sid}_2026-10.zim"
        catalog = mutate(request, catalog, "add", {
            "title": sid, "format": "zim", "source_url": prefix, "edition": "2026-10",
        }, sid=sid)
        config = source(catalog, sid)["configuration"]
        artifact = archives.install_stream(sid, config, BytesIO(old_data))
        installed[sid] = dict(source_id=sid, state="completed", active=False, verified=True,
                              configuration=jobs.identity(config), artifact=artifact)
        jobs.save(installed[sid])
        payload = {"state": "update_available", "installed_edition": "2026-10", "candidate_edition": "2026-11",
                   "candidate_url": prefix.replace("2026-10", "2026-11"),
                   "candidate_sha256": new_digest, "candidate_bytes": len(new_data)}
        updates[sid] = payload
    monkeypatch.setattr(admin_api, "_batch_available", lambda settings, formats: 10_000)
    monkeypatch.setattr(admin_api.kiwix_update_discovery, "cached_checked_source", updates.__getitem__)
    first = "bad-download"
    admin_api.update_check_store.save(first, installed[first]["artifact"]["sha256"], updates[first])
    assert 'role="status">An update is available</strong>' in request("/admin")
    for sid in updates:
        admin_api.update_check_store.save(sid, installed[sid]["artifact"]["sha256"], updates[sid])
    page = request("/admin/sources")
    assert 'role="status">Updates are available</strong>' in page
    assert 'href="/admin/sources#update-review"' in page
    for sid in ("bad-reader", "ready-public"):
        publication.save(sid, True)

    class PublisherStream(BytesIO):
        total_size = len(new_data)
        etag = '"fixture-update"'
        last_modified = None

    @contextmanager
    def download(url, **kwargs):
        sid = next(sid for sid, update in updates.items() if update["candidate_url"] == url)
        assert jobs.read(sid) == installed[sid]
        yield PublisherStream(old_data if sid == "bad-download" else new_data)

    monkeypatch.setattr(archives.pdf_transport, "open_pdf", download)
    rejected = {"missing-source": new_digest}
    selection = {sid: new_digest for sid in updates}
    batch = request("/api/update-batch", {"operation": "download-update", "revision": catalog["revision"],
                                         "selection": selection | rejected}, status=202)
    assert set(batch["rejected"]) == rejected.keys()
    assert batch["queued"].keys() == selection.keys()
    assert all(jobs.read_candidate(sid) is None for sid in updates)
    shutdown = threading.Event()
    for sid in updates:
        assert queue.run_next(admin_api._execute_batch_job, shutdown)
        assert jobs.read(sid) == installed[sid]
    assert {job["source_id"]: job["state"] for job in queue.status()} == {
        "bad-download": "failed", "bad-reader": "completed", "ready-public": "completed", "ready-private": "completed",
    }

    def reconcile(*args, source_id, configuration):
        if source_id == "bad-reader":
            jobs.rollback_candidate(source_id, configuration, reason="Fixture reader unavailable")
            return False
        assert source_id == "ready-public"
        return True

    monkeypatch.setattr(admin_api.zim_refresh, "reconcile_activation", reconcile)
    latest = request("/api/sources")
    batch = request("/api/update-batch", {"operation": "publish-update", "revision": latest["revision"],
                                         "selection": selection}, status=202)
    assert set(batch["rejected"]) == {"bad-download"}
    assert set(batch["queued"]) == {"bad-reader", "ready-public", "ready-private"}
    for _ in batch["queued"]:
        assert queue.run_next(admin_api._execute_batch_job, shutdown)
    assert {job["source_id"]: job["state"] for job in queue.status() if job["operation"] == "publish-update"} == {
        "bad-reader": "failed", "ready-public": "completed", "ready-private": "completed",
    }
    for sid in ("bad-download", "bad-reader"):
        assert jobs.read(sid) == installed[sid]
    for sid in ("ready-public", "ready-private"):
        assert jobs.read(sid)["artifact"]["sha256"] == new_digest
    assert publication.load() == {"bad-reader": True, "ready-public": True}
    for old in installed.values():
        assert (disk / "library/zim" / old["artifact"]["filename"]).read_bytes() == old_data


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "stale", "storage", "staged", "window"])
def test_automatic_replacement(app, disk, monkeypatch, outcome):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    import hashlib
    from io import BytesIO
    from theaxiom import acquisition_queue as queue, zim_jobs as jobs, zim_source as archives

    request, _ = app
    sid = "automatic-fixture"
    url = f"https://download.kiwix.org/zim/zimit/{sid}_2026-10.zim"
    catalog = mutate(request, request("/api/sources"), "add", {
        "title": sid, "format": "zim", "source_url": url, "update_policy": "scheduled",
        "update_window_start": "23:00", "update_window_end": "02:00",
    }, sid=sid)
    config = source(catalog, sid)["configuration"]
    old = fixture_bytes()
    new_body = old[:103] + b"x"
    new = new_body + hashlib.md5(new_body, usedforsecurity=False).digest()
    digest = hashlib.sha256(new).hexdigest()
    artifact = archives.install_stream(sid, config, BytesIO(old))
    installed = dict(source_id=sid, state="completed", active=False, verified=True,
                     configuration=jobs.identity(config), artifact=artifact)
    evidence = dict(state="update_available", installed_edition="2026-10", candidate_edition="2026-11",
                    candidate_url=url.replace("2026-10", "2026-11"), candidate_sha256=digest,
                    candidate_bytes=len(new))
    monkeypatch.setattr(admin_api.kiwix_update_discovery, "cached_checked_source", lambda _: evidence)
    hour = 23

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 26 if hour == 23 else 27, hour, 30, tzinfo=tz)

    monkeypatch.setattr(admin_api, "datetime", Clock)
    monkeypatch.setattr(admin_api.update_schedule, "timezone", lambda: timezone.utc)
    available = 10_000
    monkeypatch.setattr(admin_api, "_batch_available", lambda *_: available)
    admin_api._poll_automatic_updates()
    assert not queue.status()  # file alone is not a verified install
    jobs.save(installed)
    publication.save(sid, True)
    assert request("/api/automatic-update-pause", {"paused": True}) == {"paused": True}
    admin_api._poll_automatic_updates()
    assert not queue.status()
    assert request("/api/automatic-update-pause", {"paused": False}) == {"paused": False}
    hour = 2
    admin_api._poll_automatic_updates()
    assert not queue.status()
    hour = 0
    admin_api._poll_automatic_updates()
    admin_api._poll_automatic_updates()
    job, = queue.status()
    assert job["operation"] == "automatic-update"

    class PublisherStream(BytesIO):
        total_size = len(new)
        etag = '"fixture-automatic"'
        last_modified = None

    @contextmanager
    def download(*args, **kwargs):
        nonlocal hour
        assert jobs.read(sid) == installed
        assert outcome in {"success", "failure"}
        hour = 2  # Transfer was started inside the window; verification finishes outside it.
        yield PublisherStream(new if outcome == "success" else old)

    monkeypatch.setattr(archives.pdf_transport, "open_pdf", download)
    monkeypatch.setattr(admin_api.zim_refresh, "reconcile_activation", lambda *a, **kw: True)
    if outcome == "cancel":
        queue.cancel(job["id"])
    elif outcome == "storage":
        available = 0
    elif outcome == "stale":
        evidence = {"state": "unable_to_determine", "stale": True}
    elif outcome == "staged":
        jobs.prepare_candidate(sid, config | admin_api._reviewed_update_fields(evidence, digest))
    elif outcome == "window":
        hour = 2

    queue.run_next(admin_api._execute_batch_job, threading.Event())
    expected = {"success": "completed", "cancel": "cancelled", "storage": "queued", "window": "queued"}.get(outcome, "failed")
    assert queue.status()[0]["state"] == expected
    assert queue.automatic_attempts()[0]["state"] == expected
    if outcome == "success":
        current = jobs.read(sid)
        assert current and current["artifact"]["sha256"] == digest
    else:
        assert jobs.read(sid) == installed
    if outcome == "staged":
        candidate = jobs.read_candidate(sid)
        assert candidate and "automatic_job" not in candidate
    assert publication.load()[sid] is True
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == old
    hour = 23
    admin_api._poll_automatic_updates()
    assert len(queue.status()) == 1


@pytest.mark.parametrize("policy,legacy", [("automatic", False), ("scheduled", True)])
def test_rearm_rolled_back_automatic_update(app, disk, monkeypatch, policy, legacy):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    import hashlib
    from io import BytesIO
    from theaxiom import acquisition_queue as queue, update_check_store, zim_jobs as jobs, zim_source as archives

    request, _ = app
    sid = "devdocs"
    base = "https://archive.org/download/devdocs/devdocs_2026-10-01.zim"
    old = fixture_bytes()
    body = old[:103] + b"x"
    new = body + hashlib.md5(body, usedforsecurity=False).digest()
    digest = hashlib.sha1(new, usedforsecurity=False).hexdigest()
    catalog = mutate(request, request("/api/sources"), "add", {
        "title": "DevDocs", "format": "zim", "source_url": base, "update_method": "archive",
        "publisher_sha1": hashlib.sha1(old, usedforsecurity=False).hexdigest(), "update_policy": policy,
        **({"update_window_start": "23:00", "update_window_end": "02:00"} if policy == "scheduled" else {}),
    }, sid=sid)
    config = source(catalog, sid)["configuration"]
    artifact = archives.install_stream(sid, config, BytesIO(old))
    old_job = dict(source_id=sid, state="completed", active=False, verified=True,
                   configuration=jobs.identity(config), artifact=artifact)
    jobs.save(old_job)
    evidence = dict(state="update_available", evidence="fixture", installed_edition="2026-10-01",
                    candidate_edition="2026-11-01", candidate_url=base.replace("2026-10", "2026-11"),
                    candidate_sha1=digest, candidate_bytes=len(new))
    update_check_store.save(sid, artifact["sha256"], evidence)
    hour = 23

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 26, hour, 30, tzinfo=tz)

    monkeypatch.setattr(admin_api, "datetime", Clock)
    monkeypatch.setattr(admin_api.update_schedule, "timezone", lambda: timezone.utc)
    monkeypatch.setattr(admin_api, "_batch_available", lambda *_: 10_000)
    monkeypatch.setattr(admin_api.zim_refresh, "reconcile_activation", lambda *a, **kw: True)
    monkeypatch.setattr(admin_api.zim_refresh, "schedule", lambda *a, **kw: None)
    transfers = []

    class Stream(BytesIO):
        total_size = len(new)
        etag = '"fixture"'
        last_modified = None

    @contextmanager
    def download(*args, **kwargs):
        transfers.append(args)
        assert len(transfers) == 1
        yield Stream(new)

    monkeypatch.setattr(archives.pdf_transport, "open_pdf", download)
    admin_api._poll_automatic_updates()
    assert queue.run_next(admin_api._execute_batch_job, threading.Event()), queue.status()
    first, = queue.status()
    assert first["state"] == "completed", first
    catalog = request("/api/sources")
    publication.save(sid, True)
    request("/api/action", dict(action="zim-replacement-rollback", source_id=sid,
                                revision=catalog["revision"], confirm_restart=True), status=202)
    assert jobs.read(sid) == old_job
    if legacy:
        candidate = jobs.read_candidate(sid)
        assert candidate
        candidate.pop("rollback_id")
        jobs._save_candidate(candidate)  # Baseline on-disk format.
    catalog = request("/api/sources")
    status = source(catalog, sid)["automatic_update"]
    assert "rolled back" in status["reason"]
    assert status["digest"] == digest
    assert not source(catalog)["automatic_update"].get("token")
    admin_api._poll_automatic_updates()
    assert queue.status() == [first]
    action = dict(action="allow-automatic-update", source_id=sid, revision=catalog["revision"],
                  digest=digest, token=status["token"])
    request("/api/action", action, status=403, origin="https://other.invalid")
    request("/api/action", action | {"revision": catalog["revision"] + 1}, status=409)
    request("/api/action", action | {"digest": "0" * 40}, status=409)
    request("/api/action", action | {"token": "0" * 64}, status=409)
    with monkeypatch.context() as stale:
        stale.setattr(update_check_store, "MAX_AGE", -1)
        request("/api/action", action, status=409)
    queue.enqueue([dict(source_id=sid, title=sid, operation="publish-update", configuration=config, bytes=0)],
                  settings=storage_settings.load_settings(), available=None)
    pending = queue.status()[0]
    request("/api/action", action, status=409)
    assert queue.status()[0] == pending
    queue.cancel(pending["id"])
    update_check_store.save(sid, artifact["sha256"], evidence | {"candidate_sha1": "0" * 40})
    request("/api/action", action, status=409)
    update_check_store.save(sid, artifact["sha256"], evidence)
    request("/api/automatic-update-pause", {"paused": True})
    assert request("/api/action", action) == {"allowed": True}
    request("/api/action", action, status=409)
    assert queue.automatic_attempts()[0]["rollback_token"] == status["token"]
    assert "waiting for eligibility" in source(request("/api/sources"), sid)["automatic_update"]["reason"]
    admin_api._poll_automatic_updates()
    assert jobs.read(sid) == old_job
    assert len(queue.status()) == 2
    request("/api/automatic-update-pause", {"paused": False})
    if policy == "scheduled":
        hour = 2
        admin_api._poll_automatic_updates()
        assert len(queue.status()) == 2
        hour = 0
    admin_api._poll_automatic_updates()
    admin_api._poll_automatic_updates()
    assert len(queue.status()) == 3
    assert queue.run_next(admin_api._execute_batch_job, threading.Event())
    second = queue.status()[0]
    assert second["state"] == "completed", second
    live = jobs.read(sid)
    assert live and live["artifact"]["sha256"] == hashlib.sha256(new).hexdigest()
    assert len(transfers) == 1
    assert publication.load()[sid] is True
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == old
    catalog = request("/api/sources")
    request("/api/action", dict(action="zim-replacement-rollback", source_id=sid,
                                revision=catalog["revision"], confirm_restart=True), status=202)
    request("/api/action", action | {"revision": request("/api/sources")["revision"]}, status=409)
    admin_api._poll_automatic_updates()
    assert len(queue.status()) == 3
    assert jobs.read(sid) == old_job


def test_global_search_opt_out_preserves_library_entry(app, monkeypatch):
    request, _ = app
    catalog = request("/api/sources")
    catalog = mutate(request, catalog, "edit", {"search_enabled": "no"})
    assert source(catalog)["configuration"]["search_enabled"] == "no"
    publish(request, True)
    assert "Wikipedia" in request("/")
    assert f'data-source="{SID}"' not in request("/search?q=water&mode=progressive")
    request(f"/search?q=water&mode=source&source={SID}", status=404)
    catalog = mutate(request, request("/api/sources"), "edit", {"search_enabled": "yes"})
    assert f'data-source="{SID}"' in request("/search?q=water&mode=progressive")
