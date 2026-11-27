"""Document replacement through real catalog, reader and shared queue fixtures."""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import threading

import pytest

from theaxiom import (acquisition_queue, admin_api, document_origin_evidence,
                      document_replacement as replacement, document_update_discovery,
                      epub_source, pdf_source, pdf_transport, publication, update_check_store,
                      update_schedule)
from test_epub_sources import book, disk  # noqa: F401
from test_pdf_sources import document
from test_source_profiles_http import app, mutate, source  # noqa: F401

SID = "document-update-fixture"
OLD_TAG = '"old-document"'
NEW_TAG = '"new-document"'


@pytest.fixture(params=["pdf", "epub"])
def fixture(request, app, disk, monkeypatch):  # noqa: F811
    kind = request.param
    http, _ = app
    old = document(b"Old content") if kind == "pdf" else book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body>Old content</body></html>')
    new = document(b"New content") if kind == "pdf" else book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body>New content</body></html>')
    url = f"https://publisher.org/document.{kind}"
    config = dict(title="Document update fixture", format=kind, source_url=url,
                  provenance="Fixture origin", license="Fixture redistribution authorized",
                  expected_size_bytes=str(len(old)), sha256=sha256(old).hexdigest(), update_policy="notify")
    mutate(http, http("/api/sources"), "add", config, sid=SID)
    adapter = pdf_source if kind == "pdf" else epub_source
    adapter.install_stream(SID, BytesIO(old))
    document_origin_evidence.save(SID, kind, url, config["sha256"], OLD_TAG)
    record = lambda: source(admin_api.SOURCE_CATALOG.get(), SID)
    route = adapter.ROUTE_PREFIX + SID + ("/document.pdf" if kind == "pdf" else "/document.epub")
    calls = []

    def metadata(requested, **kwargs):
        assert requested == url
        tag = kwargs.get("if_none_match")
        if tag == NEW_TAG:
            return dict(source_url=url, status=304, reachable=True, etag="")
        assert tag == OLD_TAG
        return dict(source_url=url, status=200, reachable=True, etag=NEW_TAG,
                    content_length=str(len(new)))

    @contextmanager
    def transfer(requested, *, format_name, required_etag):
        assert requested == url and format_name == kind and required_etag == NEW_TAG
        calls.append(requested)
        stream = BytesIO(new)
        stream.etag = NEW_TAG
        yield stream

    monkeypatch.setattr(pdf_transport, "probe_document_metadata", metadata)
    monkeypatch.setattr(pdf_transport, "open_pdf", transfer)
    # All fixtures use the existing byte-stream validation, not real network.
    monkeypatch.setattr(admin_api, "_batch_available", lambda *args: 1 << 30)
    return dict(http=http, old=old, new=new, kind=kind, record=record, route=route, calls=calls,
                adapter=adapter, url=url, config=config)


def check(f):
    outcome = document_update_discovery.check_installed_document(f["record"]())
    assert outcome["state"] == "representation_changed"
    assert outcome["candidate_bytes"] == len(f["new"])
    assert "candidate_sha256" not in outcome
    return outcome["candidate_token"]


def run_queue():
    return acquisition_queue.run_next(admin_api._execute_batch_job, threading.Event())


def policy(f, value, **fields):
    http = f["http"]
    return mutate(http, http("/api/sources"), "edit", dict(update_policy=value, **fields), sid=SID)


def publish(f, enabled):
    http = f["http"]
    return http("/api/publication", dict(source_id=SID, enabled=enabled,
                                        revision=http("/api/sources")["revision"]))


def action(f, name, **fields):
    http = f["http"]
    return http("/api/action", dict(action=name, source_id=SID,
                                    revision=http("/api/sources")["revision"], **fields),
                status=202 if name == "zim-replacement-update" else 200)


@pytest.mark.parametrize("published", [False, True])
def test_manual_stage_publish_and_rollback(fixture, published):
    f = fixture
    http = f["http"]
    if published:
        publish(f, True)
        assert http(f["route"], raw=True)[0] == f["old"]
    token = check(f)
    view = source(http("/api/sources"), SID)
    assert view["update_check"]["candidate_token"] == token
    assert "candidate_etag" not in view["update_check"]
    action(f, "zim-replacement-update", candidate_sha256=token)
    assert run_queue()
    view = source(http("/api/sources"), SID)
    assert view["replacement"]["state"] == "verified"
    assert view["artifact"]["sha256"] == sha256(f["old"]).hexdigest()
    assert view["publication"]["enabled"] == published
    if published:
        assert http(f["route"], raw=True)[0] == f["old"]
    reviewed = view["replacement"]["review_token"]
    http("/api/action", dict(action="zim-replacement-activate", source_id=SID,
         revision=http("/api/sources")["revision"], confirm_restart=True,
         expected_replacement="0" * 64), status=409)
    assert replacement.status(f["record"]())["state"] == "verified"
    action(f, "zim-replacement-activate", confirm_restart=True, expected_replacement=reviewed)
    assert replacement.status(f["record"]())["state"] == "activated"
    assert replacement.installed(f["record"](), verify=True)["sha256"] == sha256(f["new"]).hexdigest()
    assert publication.load().get(SID, False) == published
    if published:
        assert http(f["route"], raw=True)[0] == f["new"]
    http("/api/action", dict(action="zim-replacement-rollback", source_id=SID,
         revision=http("/api/sources")["revision"], confirm_restart=True,
         expected_replacement=reviewed), status=409)
    assert replacement.status(f["record"]())["state"] == "activated"
    action(f, "zim-replacement-rollback", confirm_restart=True,
           expected_replacement=replacement.status(f["record"]())["review_token"])
    assert replacement.status(f["record"]())["state"] == "rolled_back"
    assert replacement.installed(f["record"](), verify=True)["sha256"] == sha256(f["old"]).hexdigest()
    if published:
        assert http(f["route"], raw=True)[0] == f["old"]
    action(f, "zim-replacement-delete-verified", candidate_sha256=sha256(f["new"]).hexdigest(), confirm=True)
    assert replacement.status(f["record"]()) is None
    assert check(f) == token
    assert f["calls"] == [f["url"]]


@pytest.mark.parametrize("mode", ["automatic", "scheduled"])
def test_unattended_and_rollback_retry(fixture, mode):
    f = fixture
    publish(f, True)
    now = datetime.now(update_schedule.timezone())
    fields = {}
    if mode == "scheduled":
        # Inclusive current minute, exclusive following minute.
        from datetime import timedelta
        fields = dict(update_window_start=(now - timedelta(minutes=1)).strftime("%H:%M"),
                      update_window_end=(now + timedelta(minutes=2)).strftime("%H:%M"))
    policy(f, mode, **fields)
    check(f)
    admin_api._poll_automatic_updates()
    assert run_queue()
    assert replacement.status(f["record"]())["state"] == "activated"
    assert f["http"](f["route"], raw=True)[0] == f["new"]
    action(f, "zim-replacement-rollback", confirm_restart=True)
    check(f)
    admin_api._poll_automatic_updates()
    assert run_queue()
    assert replacement.status(f["record"]())["state"] == "activated"
    assert len(f["calls"]) == 1  # Reuse verified retained bytes.
    action(f, "zim-replacement-rollback", confirm_restart=True)
    action(f, "zim-replacement-delete-verified", candidate_sha256=sha256(f["new"]).hexdigest(), confirm=True)
    check(f)
    admin_api._poll_automatic_updates()
    assert run_queue()
    assert f["http"](f["route"], raw=True)[0] == f["new"]
    assert len(f["calls"]) == 2
    admin_api._poll_automatic_updates()
    assert not run_queue()


def test_failed_candidate_and_cancel_keep_serving(fixture, monkeypatch):
    f = fixture
    publish(f, True)
    token = check(f)

    @contextmanager
    def bad(*args, **kwargs):
        stream = BytesIO(b"x" * len(f["new"]))
        stream.etag = NEW_TAG
        yield stream

    monkeypatch.setattr(pdf_transport, "open_pdf", bad)
    action(f, "zim-replacement-update", candidate_sha256=token)
    assert run_queue()
    assert acquisition_queue.status()[0]["state"] == "failed"
    assert replacement.status(f["record"]()) is None
    assert f["http"](f["route"], raw=True)[0] == f["old"]
    action(f, "zim-replacement-update", candidate_sha256=token)
    acquisition_queue.cancel(acquisition_queue.status()[0]["id"])
    assert not run_queue()
    assert f["http"](f["route"], raw=True)[0] == f["old"]


def test_notify_disabled_and_insufficient_space(fixture, monkeypatch):
    f = fixture
    token = check(f)
    admin_api._poll_automatic_updates()
    assert not run_queue()
    policy(f, "disabled")
    admin_api._poll_automatic_updates()
    assert not run_queue()
    policy(f, "automatic")
    monkeypatch.setattr(admin_api, "_batch_available", lambda *args: 0)
    admin_api._poll_automatic_updates()
    assert not run_queue()
    assert not f["calls"]
    assert replacement.installed(f["record"](), verify=True)["sha256"] == sha256(f["old"]).hexdigest()


def test_batch_update_preserves_old_until_publish(fixture):
    f = fixture
    http = f["http"]
    publish(f, True)
    token = check(f)
    http("/api/update-batch", dict(operation="download-update", selection={SID:token},
                                  revision=http("/api/sources")["revision"]), status=202)
    assert run_queue()
    assert http(f["route"], raw=True)[0] == f["old"]
    http("/api/update-batch", dict(operation="publish-update", selection={SID:sha256(f["new"]).hexdigest()},
                                  revision=http("/api/sources")["revision"]), status=202)
    assert run_queue()
    assert http(f["route"], raw=True)[0] == f["new"]


def test_unreviewed_validator_never_installed(fixture, monkeypatch):
    f = fixture
    token = check(f)

    @contextmanager
    def changed(*args, **kwargs):
        stream = BytesIO(f["new"])
        stream.etag = '"unexpected"'
        yield stream

    monkeypatch.setattr(pdf_transport, "open_pdf", changed)
    action(f, "zim-replacement-update", candidate_sha256=token)
    assert run_queue()
    assert acquisition_queue.status()[0]["state"] == "failed"
    assert replacement.installed(f["record"](), verify=True)["sha256"] == sha256(f["old"]).hexdigest()
