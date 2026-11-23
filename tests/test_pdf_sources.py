"""Generic PDF lifecycle using locally generated documents and loopback only."""
import hashlib
import os

import pytest

from theaxiom import admin_api, catalog_store, pdf_source, storage, storage_settings
from test_source_profiles_http import app as pdf_app, mutate, source  # noqa: F401

SID = "fixture-guide"
READER = pdf_source.ROUTE_PREFIX + SID


def document(marker=b"Fixture"):
    data = b"%PDF-1.4\n"
    entries = []
    for number, obj in enumerate((
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        b"<< /Title (" + marker + b") >>",
    ), 1):
        entries.append(len(data))
        data += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 5\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in entries)
    return data + f"trailer\n<< /Size 5 /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def fields(data):
    return dict(title="A local <guide>", format="pdf", source_url="https://publisher.example.org/guide.pdf",
                provenance="Locally generated fixture", license="Fixture redistribution authorized",
                expected_size_bytes=str(len(data)), sha256=hashlib.sha256(data).hexdigest())


@pytest.fixture
def disk(tmp_path, monkeypatch):
    root = tmp_path / "disk"
    inbox, library = root / "staging/pdf", root / "library/pdf"
    inbox.mkdir(mode=0o700, parents=True)
    library.mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(root)))
    # Keep actual openat and mount-ID checks, but allow fixture workdirs on overlayfs.
    mount = storage._covering(root, storage.read_mounts())
    monkeypatch.setattr(pdf_source, "LOCAL_FILESYSTEMS", {mount["type"]})
    return inbox / (SID + ".pdf"), library


def test_pdf_lifecycle(pdf_app, disk, tmp_path, monkeypatch):  # noqa: F811
    request, snapshot = pdf_app
    snapshot.update(stale=True, acquisitions=[])
    inbox, library = disk
    data = document()
    config = fields(data)
    catalog = mutate(request, request("/api/sources"), "add", config, sid=SID)
    revision = catalog["revision"]
    assert source(catalog, SID)["adapter"] == "pdf"
    page = request("/admin/sources")
    assert ">Upload PDF</button>" not in page and ">Download from URL</button>" in page
    assert "data-pdf-import" not in page
    action = dict(source_id=SID, action="import", revision=revision)
    publication = dict(source_id=SID, enabled=True, revision=revision)
    request("/api/publication", publication, status=409)
    request(READER, status=403)
    request(READER + "/document.pdf", status=403)
    request("/api/action", dict(action, path="/etc/passwd"), status=400)
    request("/api/action", dict(action, source_id="../fixture-guide"), status=400)
    request("/api/action", dict(action, revision=revision - 1), status=409)
    request("/api/action", action, status=503)
    inbox.symlink_to(tmp_path / "outside.pdf")
    (tmp_path / "outside.pdf").write_bytes(data)
    request("/api/action", action, status=503)
    inbox.unlink()
    inbox.write_bytes(data)
    inbox.chmod(0o600)
    assert request("/api/action", action)["installed_verified"]
    output = library / f"{SID}.{config['sha256']}.pdf"
    assert output.read_bytes() == data
    assert not list(inbox.parent.glob(".import-*"))
    request(READER + "/document.pdf", status=403)
    assert READER not in request("/")
    request("/api/publication", publication)
    monkeypatch.setattr(admin_api, "SOURCE_CATALOG", catalog_store.CatalogStore(tmp_path / "sources.json"))
    assert ('href="' + READER + '/document.pdf"') not in request("/")
    assert ('href="' + READER + '"') in request("/")
    assert "Open PDF" in request("/")
    page = request(READER)
    assert 'class="library-return" href="/">← Back to library</a>' in page
    assert 'A local &lt;guide&gt;' in page
    assert 'nav a.library-return{background:#18362d;color:#fff' in page
    assert 'min-height:44px' in page and 'nav a:focus-visible' in page
    assert READER + "/document.pdf" in page
    assert "/api/pdf-reader.mjs" in page
    assert "Content-Security-Policy" in request(READER, raw=True)[1]
    asset, headers = request(READER + "/document.pdf", raw=True)
    assert asset == data and headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith("inline;") and headers["Cache-Control"] == "no-store"

    request("/api/action", action, status=403, origin="https://wrong.example.org")
    request("/api/action", action, status=403, origin="")

    mutate(request, catalog, "edit", {"license": "new license"}, sid=SID, status=409)
    inbox.write_bytes(document(b"Changed"))
    request("/api/action", action, status=409)
    assert not list(inbox.parent.glob(".import-*"))
    assert output.read_bytes() == data
    assert request(READER + "/document.pdf", raw=True)[0] == data
    output.write_bytes(document(b"Changed"))
    request(READER + "/document.pdf", status=403)
    assert READER not in request("/")
    inbox.write_bytes(data)
    request("/api/action", action)
    removed = mutate(request, catalog, "remove", sid=SID)
    assert not any(row["source_id"] == SID for row in removed["sources"])
    request(READER + "/document.pdf", status=403)
    request(READER, status=403)
    assert READER not in request("/")
    assert output.read_bytes() == data


@pytest.mark.parametrize("url", ["http://publisher.org/file.pdf", "https://127.0.0.1/file.pdf",
    "https://[::1]/file.pdf", "https://user@publisher.org/file.pdf", "https://publisher.org:443/file.pdf",
    "https://publisher.org/a/../file.pdf", "https://publisher.org/%2e/file.pdf", "https://publisher.org/file.pdf?x=1",
    "https://host.local/file.pdf", "https://publisher.org\\evil.org/file.pdf"])
def test_unsafe_provenance_url(url):
    with pytest.raises(ValueError):
        pdf_source.pin(SID, dict(fields(document()), source_url=url))


@pytest.mark.parametrize("field,value", [("license", ""), ("provenance", " "),
    ("expected_size_bytes", "0"), ("expected_size_bytes", str(pdf_source.MAX_BYTES + 1)), ("sha256", "0")])
def test_missing_or_invalid_pin(field, value):
    with pytest.raises(ValueError):
        pdf_source.pin(SID, dict(fields(document()), **{field: value}))


@pytest.mark.parametrize("data", [b"plain text", b"%PDF-1.7\n%%EOF\n", document().replace(b"xref\n", b"frog\n", 1)])
def test_non_pdf_rejected_even_with_matching_digest(disk, data):
    inbox, library = disk
    inbox.write_bytes(data)
    inbox.chmod(0o600)
    with pytest.raises(ValueError, match="PDF"):
        pdf_source.import_local(SID, pdf_source.pin(SID, fields(data)))
    assert list(library.iterdir()) == []
    assert list(inbox.parent.iterdir()) == [inbox]


def test_local_storage_guards(disk, monkeypatch):
    inbox, library = disk
    data = document()
    artifact = pdf_source.pin(SID, fields(data))
    os.mkfifo(inbox)
    with pytest.raises(ValueError, match="unsafe"):
        pdf_source.import_local(SID, artifact)
    inbox.unlink()
    inbox.write_bytes(data)
    inbox.chmod(0o600)
    real_library = library.with_name("original")
    library.rename(real_library)
    library.symlink_to(real_library, target_is_directory=True)
    with pytest.raises(OSError):
        pdf_source.import_local(SID, artifact)
    library.unlink()
    real_library.rename(library)
    with monkeypatch.context() as m:
        mounts = storage.read_mounts()
        m.setattr(storage, "read_mounts", lambda: mounts + [dict(storage._covering(library, mounts),
                                                            target=str(library), type="nfs")])
        with pytest.raises(ValueError, match="configured mount"):
            pdf_source.import_local(SID, artifact)
    settings = storage_settings.load_settings()
    storage_settings.save_settings(dict(settings, minimum_free_bytes=2**63 - 1))
    with pytest.raises(ValueError, match="capacity"):
        pdf_source.import_local(SID, artifact)
    assert list(library.iterdir()) == []


def test_explicit_nfs_mount_identity_and_local_fallback(disk, monkeypatch):
    inbox, library = disk
    data = document()
    inbox.write_bytes(data)
    inbox.chmod(0o600)
    artifact = pdf_source.pin(SID, fields(data))
    settings = storage_settings.load_settings()
    root = settings["storage_root"]
    real_mounts = storage.read_mounts()
    mount = dict(storage._covering(library, real_mounts), target=root,
                 source="192.168.0.5:/volume1/offline", type="nfs4")
    monkeypatch.setattr(storage, "read_mounts", lambda: real_mounts + [mount])
    with pytest.raises(ValueError, match="configured mount"):
        pdf_source.import_local(SID, artifact)
    settings.update(storage_mount=root, storage_source=mount["source"])
    storage_settings.save_settings(settings)
    pdf_source.import_local(SID, artifact)
    assert pdf_source.ready(artifact, verify=True)
    installed = library / artifact["filename"]
    assert installed.read_bytes() == data
    # Missing mount must not fall back to the underlying local directory.
    monkeypatch.setattr(storage, "read_mounts", lambda: real_mounts)
    assert not pdf_source.ready(artifact, verify=True)
    with pytest.raises(ValueError, match="missing or replaced"):
        pdf_source.import_local(SID, artifact)
    # Same target but wrong export also fails closed.
    monkeypatch.setattr(storage, "read_mounts", lambda: real_mounts + [
        dict(mount, source="192.168.0.5:/volume1/wrong")])
    with pytest.raises(ValueError, match="missing or replaced"):
        pdf_source.import_local(SID, artifact)
    assert installed.read_bytes() == data


def test_stream_install_observes_metadata_and_preserves_previous_bytes(disk):
    from io import BytesIO
    inbox, library = disk
    data = document()
    artifact = pdf_source.install_stream(SID, BytesIO(data))
    assert artifact["bytes"] == len(data)
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert pdf_source.ready(artifact, verify=True)
    assert (library / artifact["filename"]).read_bytes() == data
    for incoming, kwargs in [
        (b"not a PDF", {}),
        (document(b"Different"), {"expected_sha256": artifact["sha256"]}),
        (data, {"expected_size": len(data) - 1}),
        (data, {"expected_size": len(data) + 1}),
    ]:
        with pytest.raises(ValueError):
            pdf_source.install_stream(SID, BytesIO(incoming), **kwargs)
    assert list(library.iterdir()) == [library / artifact["filename"]]
    assert not list(inbox.parent.glob(".import-*"))


def test_stream_install_respects_capacity_and_maximum(disk, monkeypatch):
    from io import BytesIO
    inbox, library = disk
    monkeypatch.setattr(pdf_source, "MAX_BYTES", 32)
    with pytest.raises(ValueError, match="limit"):
        pdf_source.install_stream(SID, BytesIO(document()))
    assert not list(library.iterdir())
    assert not list(inbox.parent.glob(".import-*"))


def test_pdf_download_metadata_and_explicit_publication(pdf_app, disk, monkeypatch):
    from io import BytesIO
    request, _ = pdf_app
    data = document()
    def download(sid, url, **kwargs):
        assert url == "https://publisher.example.org/guide.pdf"
        assert kwargs.pop("capture_validator") is True
        return pdf_source.install_stream(sid, BytesIO(data), **kwargs), '"etag-from-get"'
    monkeypatch.setattr(admin_api.pdf_transport, "download", download)
    catalog = mutate(request, request("/api/sources"), "add",
                     dict(title="Remote guide", format="pdf", source_url="https://publisher.example.org/guide.pdf"), sid=SID)
    action = dict(source_id=SID, action="pdf-download", revision=catalog["revision"])
    assert "data-pdf-download" in request("/admin/sources")
    request("/api/action", dict(action, url="https://other.org/"), status=400)
    request("/api/action", action, origin="", status=403)
    request("/api/action", dict(action, revision=action["revision"]-1), status=409)
    result = request("/api/action", action)
    assert result["installed_verified"]
    catalog = request("/api/sources")
    config = source(catalog, SID)["configuration"]
    assert config["sha256"] == hashlib.sha256(data).hexdigest()
    assert config["expected_size_bytes"] == str(len(data))
    from theaxiom import document_origin_evidence
    assert document_origin_evidence.load(SID, "pdf", config["source_url"], config["sha256"]) == '"etag-from-get"'
    assert "calculated locally" in config["provenance"]
    pub = dict(source_id=SID, enabled=True, revision=catalog["revision"])
    request("/api/publication", pub, status=409)
    catalog = mutate(request, catalog, "edit", {"license": "Fixture redistribution authorized"}, sid=SID)
    request("/api/publication", dict(pub, revision=catalog["revision"]))
    request("/api/action", dict(action, revision=catalog["revision"]), status=409)
    assert request(READER + "/document.pdf", raw=True)[0] == data


def test_pdf_download_creates_missing_pdf_leaf_directories(pdf_app, disk, monkeypatch):
    from io import BytesIO
    request, _ = pdf_app
    inbox, library = disk
    inbox.parent.rmdir()
    library.rmdir()
    data = document()
    monkeypatch.setattr(admin_api.pdf_transport, "download",
                        lambda sid, url, **kwargs: (pdf_source.install_stream(sid, BytesIO(data),
                            **{k: v for k, v in kwargs.items() if k != "capture_validator"}), None))
    catalog = mutate(request, request("/api/sources"), "add", fields(data), sid=SID)
    result = request("/api/action", dict(source_id=SID, action="pdf-download", revision=catalog["revision"]))
    assert result["installed_verified"]
    assert inbox.parent.is_dir()
    assert (library / (SID + "." + hashlib.sha256(data).hexdigest() + ".pdf")).read_bytes() == data


def test_pdf_download_storage_permission_error_is_actionable(pdf_app, disk, monkeypatch):
    request, _ = pdf_app
    def denied(*args, **kwargs):
        raise PermissionError("private path must not be returned")
    monkeypatch.setattr(admin_api.pdf_transport, "download", denied)
    catalog = mutate(request, request("/api/sources"), "add", fields(document()), sid=SID)
    result = request("/api/action", dict(source_id=SID, action="pdf-download", revision=catalog["revision"]), status=503)
    assert "not writable" in result["error"]
    assert "private path" not in result["error"]


def test_raw_pdf_upload_without_url(pdf_app, disk):
    request, _ = pdf_app
    data = document()
    catalog = mutate(request, request("/api/sources"), "add",
                     dict(title="Uploaded guide", format="pdf"), sid=SID)
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(catalog["revision"])}
    def upload(body, status=200, extra=None, origin=None):
        return request("/api/pdf-upload", body, status=status, origin=origin,
                       upload_headers=headers | (extra or {}))
    upload(data, 403, origin="")
    upload(data, 403, origin="https://wrong.example.org")
    upload(data, 400, {"X-Source-ID": "../inside"})
    upload(data, 409, {"X-Catalog-Revision": str(catalog["revision"]-1)})
    upload(b"not a PDF", 409)
    assert not list(disk[1].iterdir())
    result = upload(data)
    assert result["installed_verified"]
    catalog = request("/api/sources")
    config = source(catalog, SID)["configuration"]
    assert config["sha256"] == hashlib.sha256(data).hexdigest()
    assert config["expected_size_bytes"] == str(len(data))
    assert "Uploaded by administrator" in config["provenance"]
    assert source(catalog, SID)["installed_verified"]
    assert "Review usage rights before publishing" in request("/admin/sources")
    pub = dict(source_id=SID, enabled=True, revision=catalog["revision"])
    request("/api/publication", pub, status=409)
    catalog = mutate(request, catalog, "edit", {"license": "Fixture redistribution authorized"}, sid=SID)
    request("/api/publication", dict(pub, revision=catalog["revision"]))
    upload(data, 409, {"X-Catalog-Revision": str(catalog["revision"])})
    assert request(READER + "/document.pdf", raw=True)[0] == data


def test_new_pdf_leaf_corrects_server_inherited_write_permissions(disk, monkeypatch):
    from io import BytesIO
    inbox, library = disk
    inbox.parent.rmdir()
    library.rmdir()
    mkdir = os.mkdir
    def permissive(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode=0o777 if path == "pdf" else mode, dir_fd=dir_fd)
        if path == "pdf":
            os.chmod(path, 0o777, dir_fd=dir_fd)
    monkeypatch.setattr(os, "mkdir", permissive)
    artifact = pdf_source.install_stream(SID, BytesIO(document()))
    assert pdf_source.ready(artifact, verify=True)
    assert inbox.parent.stat().st_mode & 0o777 == 0o700
    assert library.stat().st_mode & 0o777 == 0o700
    library.chmod(0o777)
    assert pdf_source.ready(artifact, verify=True)
    assert pdf_source.install_stream(SID, BytesIO(document())) == artifact
    assert library.stat().st_mode & 0o777 == 0o777


def test_add_pdf_in_one_upload(pdf_app, disk):
    from urllib.parse import quote
    import json
    request, _ = pdf_app
    catalog = request("/api/sources")
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(catalog["revision"]),
               "X-PDF-Add": "1", "X-PDF-Metadata": quote(json.dumps({"title": "My PDF"}))}
    def upload(data, status=200, extra=None):
        return request("/api/pdf-upload", data, status=status, upload_headers=headers | (extra or {}))
    upload(b"invalid PDF", 409)
    assert request("/api/sources")["revision"] == catalog["revision"]
    assert not list(disk[1].iterdir())
    upload(document(), 400, {"X-PDF-Metadata": quote(json.dumps({"title": "bad", "sha256": "fake"}))})
    assert upload(document())["installed_verified"]
    updated = request("/api/sources")
    assert updated["revision"] == catalog["revision"] + 1
    record = source(updated, SID)
    assert record["profile"]["title"] == "My PDF"
    assert record["installed_verified"]
    assert record["configuration"]["sha256"] == hashlib.sha256(document()).hexdigest()
    upload(document(), 409, {"X-Catalog-Revision": str(updated["revision"])})
    assert request("/api/sources")["revision"] == updated["revision"]
    assert 'id="source-upload-file"' in request("/admin/sources")


def test_pdf_publish_with_explicit_rights_acknowledgement(pdf_app, disk):
    from urllib.parse import quote
    import json
    request, _ = pdf_app
    catalog = request("/api/sources")
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(catalog["revision"]),
               "X-PDF-Add": "1", "X-PDF-Metadata": quote(json.dumps({"title": "My PDF"}))}
    request("/api/pdf-upload", document(), upload_headers=headers)
    catalog = request("/api/sources")
    record = source(catalog, SID)
    assert record["installed_verified"]
    assert record["publication"]["requires_rights_acknowledgement"]
    assert not record["publication"]["published"]
    pub = dict(source_id=SID, enabled=True, revision=catalog["revision"])
    request("/api/publication", pub, status=409)
    request("/api/publication", dict(pub, acknowledge_rights=False), status=409)
    request("/api/publication", dict(pub, acknowledge_rights="true"), status=400)
    request("/api/publication", dict(pub, acknowledge_rights=True))
    updated = request("/api/sources")
    record = source(updated, SID)
    assert record["publication"]["published"]
    assert record["publication"]["entry_path"] == READER
    assert "Administrator confirmed permission" in record["configuration"]["license"]
    assert request(READER + "/document.pdf", raw=True)[0] == document()
    request("/api/publication", dict(pub, enabled=False, revision=updated["revision"]))
    assert not source(request("/api/sources"), SID)["publication"]["published"]


def test_admin_pdf_header_shows_installed_size(pdf_app, disk):
    request, _ = pdf_app
    data = document()
    catalog = mutate(request, request("/api/sources"), "add", fields(data), sid=SID)
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(catalog["revision"])}
    request("/api/pdf-upload", data, upload_headers=headers)
    page = request("/admin/sources")
    assert "PDF verified" not in page
    assert "Archive verified" not in page
    assert "PDF pin verified" not in page
    assert "Installed verification" not in page
    assert 'data-field="status"' in page


@pytest.mark.parametrize("marker,expected", [
    (b"Emergency Field Guide", "Emergency Field Guide"),
    (b"  A   useful title  ", "A useful title"),
    (b"Untitled", None),
    (b"", None),
    (b"Title with \\(escape\\)", None),
    (b"Not ASCII \xff", None),
])
def test_simple_pdf_info_title(marker, expected):
    assert pdf_source.simple_document_title(document(marker)) == expected


def test_simple_pdf_title_requires_xref():
    data = document(b"Trusted title")
    assert pdf_source.simple_document_title(data.replace(b"/Info 4 0 R", b"/Info 3 0 R")) is None
    assert pdf_source.simple_document_title(data.replace(b"xref\n", b"xxxx\n", 1)) is None
    assert pdf_source.simple_document_title(b"/Title (Misleading)") is None


@pytest.mark.parametrize("automatic,expected", [(True, "Embedded title"), (False, "Manual title")])
def test_new_pdf_prefers_simple_metadata_only_when_requested(pdf_app, disk, automatic, expected):
    import json
    from urllib.parse import quote
    request, _ = pdf_app
    catalog = request("/api/sources")
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(catalog["revision"]),
               "X-PDF-Add": "1", "X-PDF-Metadata": quote(json.dumps({"title": "Manual title"}))}
    if automatic:
        headers["X-PDF-Auto-Title"] = "1"
    request("/api/pdf-upload", document(b"Embedded title"), upload_headers=headers)
    assert source(request("/api/sources"), SID)["profile"]["title"] == expected


def test_pdfjs_assets_are_local_and_restricted(pdf_app, tmp_path, monkeypatch):
    root = tmp_path / "pdfjs"
    build = root / "build"
    build.mkdir(parents=True)
    (build / "pdf.worker.mjs").write_text("// worker fixture")
    (build / "pdf.mjs").write_text("// library fixture")
    (build / "private.txt").write_text("not public")
    outside = tmp_path / "outside.mjs"
    outside.write_text("private")
    (build / "escape.mjs").symlink_to(outside)
    monkeypatch.setattr(admin_api, "PDFJS_ROOT", root)
    request, _ = pdf_app
    body, headers = request("/api/pdfjs/build/pdf.worker.mjs", raw=True)
    assert b"worker fixture" in body
    assert headers["Content-Type"] == "text/javascript"
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    request("/api/pdfjs/build/private.txt", status=404)
    request("/api/pdfjs/build/escape.mjs", status=404)
    request("/api/pdfjs/build/../outside.mjs", status=404)


def test_pdf_search_occurrences_and_navigation():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    script = (Path(__file__).parents[1] / "templates/pdf-reader.mjs").read_text()
    helpers = script[script.index("function pageMatches("):script.index("async function draw(")]
    subprocess.run([node, "-e", helpers + r"""
const assert = require("node:assert/strict");
const matches = pageMatches([{str:"Wa"},{str:"ter water"},{str:"water",hasEOL:true},{str:"safe"}],"WATER");
assert.equal(matches.length,3);
assert.deepEqual(matches[0],[{i:0,start:0,end:2},{i:1,start:0,end:3}]);
assert.equal(pageMatches([{str:"water",hasEOL:true},{str:"safe"}],"water safe").length,1);
assert.equal(pageMatches([{str:"water"}],"").length,0);
assert.equal(pageMatches([{str:"water"}],"fire").length,0);
assert.equal(wrappedMatch(0,-1,3),2);
assert.equal(wrappedMatch(2,1,3),0);
"""], check=True)


def test_pdf_canvas_and_text_layer_are_isolated():
    from pathlib import Path
    root = Path(__file__).parents[1] / "templates"
    html = (root / "pdf-reader.html.j2").read_text()
    script = (root / "pdf-reader.mjs").read_text()
    assert '/web/pdf_viewer.css' not in html
    assert '#paper{display:block;position:relative;z-index:0' in html
    assert '#text-layer{position:absolute;inset:0;z-index:1;opacity:0' in html
    assert '#highlights{position:absolute;inset:0;z-index:2' in html
    show = script[script.index("async function show("):script.index("async function thumbnails(")]
    assert 'const canvas = document.createElement("canvas")' in show
    assert 'await task.promise;' in show
    assert show.index('await task.promise;') < show.index('$("paper").replaceWith(canvas)') < show.index('new pdfjs.TextLayer')
    assert 'const canvas = $("paper")' not in show
    assert 'if (token !== generation) return;' in show
    assert 'const container = document.createElement("div")' in show
    assert 'Text highlights unavailable on this page.' in show
    assert 'new ResizeObserver' in script


def test_pdf_compact_toolbar_and_disclosed_search():
    from pathlib import Path
    from html.parser import HTMLParser
    root = Path(__file__).parents[1] / "templates"
    html = (root / "pdf-reader.html.j2").read_text()
    script = (root / "pdf-reader.mjs").read_text()

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.controls = {}
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                assert attrs["id"] not in self.controls
                self.controls[attrs["id"]] = (tag, attrs)

    parser = Parser()
    parser.feed(html)
    controls = parser.controls
    for key in ("toggle", "prev", "next", "out", "fit", "in", "search-toggle",
                "print", "find-prev", "find-next", "search-close"):
        tag, attrs = controls[key]
        assert tag == "button" and attrs["aria-label"] and attrs["title"]
        assert attrs["type"] == "button"
    assert "hidden" in controls["search-tools"][1]
    assert controls["search-toggle"][1]["aria-controls"] == "search-tools"
    assert controls["search-toggle"][1]["aria-expanded"] == "false"
    assert controls["page"][1]["inputmode"] == "numeric"
    assert 'id="matches" role="status" aria-live="polite"' in html
    assert '#search-tools[hidden]{display:none}' in html
    assert 'function setSearchOpen(open)' in script
    assert '$("find").value = "";' in script
    assert 'event.key === "Escape"' in script
    assert '$("search-toggle").focus();' in script


@pytest.mark.parametrize("format_name", ["pdf", "epub"])
def test_preview_counts_safe_retained_document_editions(disk, format_name):
    _, library = disk
    library = library.parent / format_name
    library.mkdir(exist_ok=True)

    def artifact(sid, digest, data):
        path = library / f"{sid}.{digest * 64}.{format_name}"
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    first = artifact(SID, "a", b"first edition")
    second = artifact(SID, "b", b"next edition")
    artifact("unselected", "c", b"unrelated")
    (library / f"unsafe.{'d' * 64}.{format_name}").symlink_to(first)
    assert pdf_source.preview_installed_sizes([SID, "empty", "unsafe"], format_name) == {
        SID: first.stat().st_size + second.stat().st_size,
        "empty": 0,
        "unsafe": None,
    }


@pytest.mark.parametrize("kind", ["pdf", "epub"])
def test_delete_downloaded_documents_preserves_profile_and_unpublishes_first(pdf_app, disk, monkeypatch, kind):
    from io import BytesIO
    from theaxiom import epub_source, publication
    from test_epub_sources import book

    request, _ = pdf_app
    _, pdf_library = disk
    (pdf_library.parent.parent / "staging").mkdir(exist_ok=True)
    body = book() if kind == "epub" else document()
    install = epub_source.install_stream if kind == "epub" else pdf_source.install_stream
    downloaded = install(SID, BytesIO(body))
    artifact = downloaded[0] if kind == "epub" else downloaded
    records = request("/api/sources")
    records = mutate(request, records, "add", {
        **fields(body), "format": kind, "source_url": f"https://example.org/guide.{kind}",
    }, sid=SID)
    directory = pdf_library.parent / kind
    target = directory / artifact["filename"]
    retained = directory / f"{SID}.{'b' * 64}.{kind}"
    retained.write_bytes(body)
    retained.chmod(0o600)
    other = directory / f"other.{'b' * 64}.{kind}"
    other.write_bytes(body)
    publication.save(SID, True)
    data = dict(action="delete-downloaded-files", source_id=SID, revision=records["revision"])

    shared = directory / "second-link"
    os.link(target, shared)
    request("/api/action", data, status=409)
    assert publication.load()[SID] and target.exists() and shared.exists()
    shared.unlink()

    plan = request("/api/action", data)
    assert plan["published"]
    assert {row["filename"] for row in plan["files"]} == {target.name, retained.name}
    request("/api/action", dict(data, confirmation="a" * 64), status=409)
    assert publication.load()[SID] and target.exists()
    unlink = pdf_source.unlink_managed_file

    def verify_unpublished(*args):
        assert not publication.load().get(SID)
        unlink(*args)

    monkeypatch.setattr(pdf_source, "unlink_managed_file", verify_unpublished)
    assert request("/api/action", dict(data, confirmation=plan["confirmation"]))["deleted"] == 2
    assert not target.exists() and not retained.exists() and other.exists()
    result = request("/api/sources")
    assert source(result, SID)["configuration"] == source(records, SID)["configuration"]
    assert not source(result, SID)["installed_verified"]
    install(SID, BytesIO(body))
    assert target.exists()
