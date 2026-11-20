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
    assert "data-pdf-import" in request("/admin/sources")
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
    assert READER in request("/")
    page = request(READER)
    assert 'href="/"' in page and 'A local &lt;guide&gt;' in page
    assert READER + "/document.pdf" in page
    asset, headers = request(READER + "/document.pdf", raw=True)
    assert asset == data and headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith("inline;") and headers["Cache-Control"] == "no-store"

    request("/api/action", action, status=403, origin="https://wrong.example.org")
    request("/api/action", action, status=403, origin="")

    mutate(request, catalog, "edit", {"license": "new license"}, sid=SID, status=409)
    mutate(request, catalog, "remove", sid=SID, status=409)
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
    request("/api/publication", dict(publication, enabled=False))
    request(READER + "/document.pdf", status=403)
    mutate(request, catalog, "remove", sid=SID)
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
        return pdf_source.install_stream(sid, BytesIO(data), **kwargs)
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
    assert "calculated locally" in config["provenance"]
    pub = dict(source_id=SID, enabled=True, revision=catalog["revision"])
    request("/api/publication", pub, status=409)
    catalog = mutate(request, catalog, "edit", {"license": "Fixture redistribution authorized"}, sid=SID)
    request("/api/publication", dict(pub, revision=catalog["revision"]))
    request("/api/action", dict(action, revision=catalog["revision"]), status=409)
    assert request(READER + "/document.pdf", raw=True)[0] == data
