"""Synthetic offline EPUB fixtures; no external dependencies."""
import io
import zipfile

import pytest

from theaxiom import epub_source


def book(*, chapter=None, extra=(), href="chapter.xhtml", version="3.0"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OPS/book.opf", f'<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="{version}"><metadata><dc:title>Offline &amp; safe</dc:title><dc:creator>Example Author</dc:creator><dc:language>en</dc:language><dc:publisher>Example Press</dc:publisher></metadata><manifest><item id="c" href="{href}" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>')
        z.writestr("OPS/chapter.xhtml", chapter if chapter is not None else '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter one</h1><p>Hello offline.</p></body></html>')
        for name, content in extra:
            z.writestr(name, content)
    return out.getvalue()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_metadata_and_spine(version):
    assert epub_source.inspect(book(version=version)) == dict(
        title="Offline & safe", author="Example Author", language="en",
        publisher="Example Press", version=version, chapters=["OPS/chapter.xhtml"])


@pytest.mark.parametrize("data", [
    b"not a zip",
    book(extra=[("../escape", b"x")]),
    book(extra=[("OPS/CHAPTER.xhtml", b"x")]),
    book(extra=[("META-INF/encryption.xml", b"x")]),
    book(extra=[("large.txt", b"x" * 1000000)]),
    book(href="https://example.org/chapter.xhtml"),
    book(href="../chapter.xhtml"),
    book(href="%2e%2e/chapter.xhtml"),
    book(href="missing.xhtml"),
    book(version="1.0"),
    book(chapter='<!DOCTYPE html [<!ENTITY x "bad">]><html xmlns="http://www.w3.org/1999/xhtml"><body>&x;</body></html>'),
    book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body><script>bad()</script></body></html>'),
    book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body onload="bad()"/></html>'),
])
def test_reject_unsafe_or_invalid(data):
    with pytest.raises(ValueError):
        epub_source.inspect(data)


def test_compressed_size_limit(monkeypatch):
    monkeypatch.setattr(epub_source, "MAX_BYTES", 10)
    with pytest.raises(ValueError):
        epub_source.inspect(book())


def test_expanded_budget(monkeypatch):
    monkeypatch.setattr(epub_source, "MAX_EXPANDED", 100)
    with pytest.raises(ValueError):
        epub_source.inspect(book())


@pytest.fixture
def disk(tmp_path, monkeypatch):
    from theaxiom import pdf_source, storage, storage_settings
    root = tmp_path / "disk"
    (root / "staging").mkdir(parents=True)
    (root / "library").mkdir()
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(root)))
    mount = storage._covering(root, storage.read_mounts())
    monkeypatch.setattr(pdf_source, "LOCAL_FILESYSTEMS", {mount["type"]})
    return root


def test_install_pin_and_verify(disk):
    import hashlib
    data = book()
    artifact, metadata = epub_source.install_stream("fixture-book", io.BytesIO(data))
    assert metadata["author"] == "Example Author"
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert artifact["bytes"] == len(data)
    assert artifact["filename"].endswith(".epub")
    config = dict(format="epub", provenance="Local fixture", license="",
                  expected_size_bytes=str(len(data)), sha256=artifact["sha256"])
    assert epub_source.pin("fixture-book", config, require_rights=False)["filename"] == artifact["filename"]
    with pytest.raises(ValueError):
        epub_source.pin("fixture-book", config)
    assert epub_source.ready(artifact)
    with epub_source.installed(artifact) as stream:
        assert stream.read() == data
    assert not list((disk / "staging/epub").iterdir())
    output = disk / "library/epub" / artifact["filename"]
    output.write_bytes(b"x" * len(data))
    assert not epub_source.ready(artifact)


def test_invalid_install_never_promotes(disk):
    for data in (b"not an epub", book(extra=[("../escape", b"x")])):
        with pytest.raises(ValueError):
            epub_source.install_stream("fixture-book", io.BytesIO(data))
    assert not list((disk / "staging/epub").iterdir())
    assert not list((disk / "library/epub").iterdir())


def test_epub_storage_refuses_symlink(disk, tmp_path):
    (disk / "library/epub").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        epub_source.install_stream("fixture-book", io.BytesIO(book()))


def test_epub_lifecycle_adapter():
    from theaxiom import admin_api
    life = admin_api.select_lifecycle("fixture-book", {"format": "epub", "source_url": "https://publisher.org/book.epub"})
    assert life["adapter"] == "epub"
    assert life["pdf_download"]
    assert not life["artifact"]
    assert not life["actions"]


from test_source_profiles_http import app as epub_app, source, mutate  # noqa: F401


def test_epub_http_upload(epub_app, disk):
    import json
    from urllib.parse import quote
    request, _ = epub_app
    catalog = request("/api/sources")
    headers = {"X-Source-ID": "fixture-book", "X-Catalog-Revision": str(catalog["revision"]),
               "X-Document-Format": "epub", "X-PDF-Add": "1", "X-PDF-Auto-Title": "1",
               "Content-Type": "application/epub+zip",
               "X-PDF-Metadata": quote(json.dumps({"title": "Filename", "author": "Manual author"}))}
    request("/api/pdf-upload", book(), upload_headers=headers, origin="", status=403)
    request("/api/pdf-upload", b"bad archive", upload_headers=headers, status=409)
    assert request("/api/sources")["revision"] == catalog["revision"]
    assert request("/api/pdf-upload", book(), upload_headers=headers)["installed_verified"]
    updated = request("/api/sources")
    row = source(updated, "fixture-book")
    assert row["adapter"] == "epub" and row["installed_verified"]
    assert row["profile"]["title"] == "Offline & safe"
    assert row["profile"]["author"] == "Manual author"
    assert row["profile"]["publisher"] == "Example Press"
    assert row["download_filter"] == "downloaded"
    request("/api/publication", dict(source_id="fixture-book", enabled=True, revision=updated["revision"]), status=400)
    assert "Upload EPUB" in request("/admin/sources")


def test_epub_http_add_download(epub_app, disk, monkeypatch):
    from contextlib import contextmanager
    from theaxiom import pdf_transport
    request, _ = epub_app
    calls = []

    @contextmanager
    def fake(url, *, format_name):
        calls.append((url, format_name))
        yield io.BytesIO(book())

    monkeypatch.setattr(pdf_transport, "open_pdf", fake)
    revision = request("/api/sources")["revision"]
    payload = dict(source_id="url-book", action="epub-add-download", revision=revision,
                   fields=dict(title="Filename", source_url="https://publisher.org/book.epub"),
                   auto_title=True)
    request("/api/action", payload, origin="", status=403)
    request("/api/action", dict(payload, revision=revision+1), status=409)
    assert not calls
    assert request("/api/action", payload)["installed_verified"]
    assert calls == [("https://publisher.org/book.epub", "epub")]
    row = source(request("/api/sources"), "url-book")
    assert row["profile"]["title"] == "Offline & safe"
    assert row["profile"]["author"] == "Example Author"
    assert row["installed_verified"]
    assert "Downloaded via configured HTTPS URL" in row["configuration"]["provenance"]
