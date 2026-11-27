import hashlib
import io
import json
from contextlib import contextmanager

import pytest

from theaxiom import admin_api, search, structured_source as data, zim_jobs, zim_source
from test_source_profiles_http import app, mutate
from test_zim_source import disk


def config(raw, kind="json", **fields):
    return dict(format=kind, source_url="https://example.org/records." + kind,
                expected_size_bytes=str(len(raw)), sha256=hashlib.sha256(raw).hexdigest(),
                **fields)


def json_fixture():
    return json.dumps({"items": [{"name": "Alpha repeater", "location": {"city": "Lowell"},
                                  "frequency": 146.52, "note": "<script>bad()</script>"},
                                 {"name": "Beta repeater", "location": {"city": "Boston"},
                                  "frequency": 440, "note": "Second record"}]}).encode()


def selected(raw):
    return config(raw, data_record_path="items",
                  data_columns="name,location.city,frequency,note", data_title_field="name")


def test_json_columns_and_local_record_search():
    raw = json_fixture()
    dataset = data.parse(io.BytesIO(raw), selected(raw))
    assert dataset.columns == ("name", "location.city", "frequency", "note")
    assert dataset.rows[0][1:3] == ("Lowell", "146.52")
    assert dataset.title(1, dataset.rows[0]) == "Alpha repeater"
    assert [n for n, _ in dataset.matching("LOWELL")] == [1]


def test_csv_unicode_quoted_fields_and_selected_order():
    raw = '\ufeffname,city,notes\r\nAlpha,Montréal,"a,b"\r\nBeta,Boston,"two\nlines"\r\n'.encode()
    dataset = data.parse(io.BytesIO(raw), config(raw, "csv", data_columns="city,name,notes",
                                                  data_title_field="name"))
    assert dataset.columns == ("city", "name", "notes")
    assert dataset.rows[0] == ("Montréal", "Alpha", "a,b")
    assert dataset.rows[1][2] == "two\nlines"


@pytest.mark.parametrize("kind,raw", [
    ("json", b'{"name":"not an array"}'),
    ("json", b'[{"name":"a","name":"b"}]'),
    ("json", b'[{"name":NaN}]'),
    ("json", b'[{"name":1e999}]'),
    ("json", b'[{"name":"\\ud800"}]'),
    ("json", b'[1,2]'),
    ("json", b'[]'),
    ("json", b'<html>error</html>'),
    ("csv", b'name,name\none,two\n'),
    ("csv", b'name,city\none\n'),
    ("csv", b'name,city\n"unfinished,one\n'),
])
def test_malformed_data_refused(kind, raw):
    with pytest.raises(ValueError):
        data.parse(io.BytesIO(raw), config(raw, kind))


def test_limits_and_options(monkeypatch):
    raw = b'[{"name":"one"},{"name":"two"}]'
    with pytest.raises(ValueError):
        data.options(config(raw, data_columns="name,name"))
    with pytest.raises(ValueError):
        data.options(config(raw, "csv", data_record_path="items"))
    with pytest.raises(ValueError):
        data.parse(io.BytesIO(raw), config(raw, data_columns="missing"))
    monkeypatch.setattr(data, "MAX_ROWS", 1)
    with pytest.raises(ValueError):
        data.parse(io.BytesIO(raw), config(raw))
    monkeypatch.setattr(data, "MAX_BYTES", 10)
    with pytest.raises(ValueError):
        zim_source.candidate(config(raw))


def test_validated_install_and_replacement_use_archive_worker(disk, monkeypatch):
    sid = "directory"
    raw = b"name,city\nAlpha,Lowell\n"
    original = config(raw, "csv")
    artifact = zim_source.install_stream(sid, original, io.BytesIO(raw))
    live = dict(source_id=sid, state="completed", active=False, verified=True,
                configuration=zim_jobs.identity(original), artifact=artifact)
    zim_jobs.save(live)
    assert artifact["bytes"] == len(raw)
    assert (disk / "library/csv" / artifact["filename"]).read_bytes() == raw
    bad = b"name,city\nbroken\n"
    with pytest.raises(ValueError):
        zim_source.install_stream("malformed", config(bad, "csv"), io.BytesIO(bad))
    new = b"name,city\nBeta,Boston\n"
    changed = config(new, "csv")
    zim_jobs.prepare_candidate(sid, changed)
    @contextmanager
    def transport(url, **kwargs):
        assert kwargs["media_type"] == "text/csv"
        stream = io.BytesIO(new)
        stream.total_size, stream.etag, stream.last_modified = len(new), '"new"', None
        yield stream
    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf", transport)
    candidate = zim_jobs.acquire_candidate(sid, changed)
    assert candidate["state"] == "verified"
    assert zim_jobs.read(sid) == live
    assert zim_jobs.activate_candidate(sid, changed)["artifact"] == candidate["artifact"]
    assert zim_jobs.rollback_candidate(sid, changed) == live
    assert (disk / "library/csv" / artifact["filename"]).read_bytes() == raw


def test_http_reader_and_global_search_publication_boundary(app, disk):
    request, _ = app
    sid = "fixture-data"
    raw = json_fixture()
    fields = dict(selected(raw), title="Local directory", publisher="Fixture publisher", license="CC0",
                  update_policy="disabled", provenance="Synthetic test fixture")
    catalog = mutate(request, request("/api/sources"), "add", fields, sid=sid)
    row = next(r for r in catalog["sources"] if r["source_id"] == sid)
    assert row["adapter"] == "structured" and row["zim_download"]
    artifact = zim_source.install_stream(sid, fields, io.BytesIO(raw))
    zim_jobs.save(dict(source_id=sid, state="completed", active=False, verified=True,
                       configuration=zim_jobs.identity(fields), artifact=artifact))
    path = "/api/library/data/" + sid
    request(path, status=403)
    request("/search?q=Lowell&source=" + sid + "&mode=source", status=404)
    request("/api/publication", dict(source_id=sid, enabled=True, revision=catalog["revision"]))
    assert "Local directory" in request("/")
    result = request(path + "?q=Lowell")
    assert "Alpha repeater" in result and "Beta repeater" not in result
    assert "&lt;script&gt;bad()&lt;/script&gt;" in result and "<script>bad()" not in result
    body, headers = request(path + "?view=table", raw=True)
    assert b"<table>" in body and b"Beta repeater" in body
    assert headers["Cache-Control"] == "no-store" and "default-src 'none'" in headers["Content-Security-Policy"]
    group = request("/search?q=Lowell&source=" + sid + "&mode=source")
    assert group["capability"] == "Data record search"
    assert len(group["results"]) == 1
    href = group["results"][0]["href"]
    assert href == path + "/record/" + artifact["sha256"] + "/1"
    assert "Alpha repeater" in request(href)
    request(path + "/record/" + "0" * 64 + "/1", status=400)
    request(path + "?page=1&page=2", status=400)
    request(path + "?view=invalid", status=400)
    revision = request("/api/sources")["revision"]
    request("/api/publication", dict(source_id=sid, enabled=False, revision=revision))
    request(href, status=403)
    request("/search?q=Lowell&source=" + sid + "&mode=source", status=404)
    assert "Local directory" not in request("/")


def test_record_link_validation_rejects_other_sources_and_digests():
    route = dict(kind="structured", path="/api/library/data/fixture",
                 snapshot={"artifact": {"sha256": "a" * 64}})
    href = route["path"] + "/record/" + "a" * 64 + "/1"
    assert search.valid_link(href, route)
    for invalid in [href + "?q=x", href + "#x", href.replace("fixture", "other"),
                    href.replace("a" * 64, "b" * 64), href.replace("/1", "/0")]:
        assert not search.valid_link(invalid, route)


def test_reader_pagination_and_record_links():
    raw = json.dumps([{"name": f"Record {i}", "city": "Lowell"} for i in range(25)]).encode()
    snapshot = dict(source_id="fixture", profile={"title": "Data"}, license="CC0",
                    artifact={"sha256": hashlib.sha256(raw).hexdigest()})
    dataset = data.parse(io.BytesIO(raw), config(raw))
    context = data.reader_context(dataset, snapshot, ("", 2, "cards", None, None))
    assert context["total"] == 25 and len(context["entries"]) == 5
    assert context["entries"][0]["index"] == 21
    assert context["previous"] and not context["following"]


def test_numeric_range_labels_are_generic_and_searchable():
    raw = b'[{"name":"First","value":5},{"name":"Edge","value":10},{"name":"Other","value":99}]'
    fields = config(raw, data_columns="name,value", data_title_field="name",
                    data_range_labels='["value","category",[[0,10,"Low"],[10,20,"Medium"]]]')
    dataset = data.parse(io.BytesIO(raw), fields)
    assert dataset.columns == ("name", "value", "category")
    assert [row[-1] for row in dataset.rows] == ["Low", "Medium", ""]
    assert [i for i, _ in dataset.matching("medium")] == [2]
    assert zim_jobs.identity(fields)["data_range_labels"] == fields["data_range_labels"]
    assert "data_range_labels" not in zim_jobs.identity(config(raw))
    for invalid in ('["value","name",[[0,10,"Low"]]]',
                    '["missing","category",[[0,10,"Low"]]]',
                    '["value","category",[[0,10,"Low"],[9,20,"High"]]]'):
        with pytest.raises(ValueError):
            data.options(dict(fields, data_range_labels=invalid))


def test_hearham_default_schema_and_opt_in(app):
    request, _ = app
    row = next(r for r in request("/api/sources")["sources"] if r["source_id"] == "hearham")
    assert row["adapter"] == "structured" and row["zim_download"]
    assert not row["publication"]["enabled"]
    assert row["profile"]["icon_png"]
    fields = row["configuration"]
    assert fields["source_url"] == "https://hearham.com/api/repeaters/v1"
    assert fields["update_policy"] == "disabled"
    assert not fields.get("sha256") and not fields.get("expected_size_bytes")
    raw = json.dumps([dict(callsign="TEST1", city="Example City", frequency=145270000,
                           offset=-600000, mode="FM", encode="100", decode="100",
                           latitude=42.0, longitude=-71.0, group="Test", internet_node="0",
                           power="unknown", operational=1, restriction="", description="Fixture")]).encode()
    dataset = data.parse(io.BytesIO(raw), fields)
    assert dataset.title(1, dataset.rows[0]) == "TEST1"
    for query in ("TEST1", "Example City", "145270000", "FM", "2m"):
        assert [n for n, _ in dataset.matching(query)] == [1]
