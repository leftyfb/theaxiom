"""Synthetic offline EPUB fixtures; no external dependencies."""
import io
import zipfile

import pytest

from theaxiom import epub_source


def book(*, chapter=None, extra=(), href="chapter.xhtml", version="3.0", manifest_extra=""):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OPS/book.opf", f'<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="{version}"><metadata><dc:title>Offline &amp; safe</dc:title><dc:creator>Example Author</dc:creator><dc:language>en</dc:language><dc:publisher>Example Press</dc:publisher></metadata><manifest><item id="c" href="{href}" media-type="application/xhtml+xml"/>{manifest_extra}</manifest><spine><itemref idref="c"/></spine></package>')
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
    request("/api/publication", dict(source_id="fixture-book", enabled=True, revision=updated["revision"]), status=409)
    page = request("/admin/sources")
    assert ">Replace EPUB</button>" in page
    assert ">Upload EPUB</button>" not in page


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


def test_epub_public_reader_and_revocation(epub_app, disk):
    import json
    from urllib.parse import quote
    request, _ = epub_app
    revision = request("/api/sources")["revision"]
    data = book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Safe chapter</h1><p><a href="https://example.org">External</a><img src="https://example.org/track" alt="Illustration"/>&lt;script&gt;</p></body></html>')
    headers = {"X-Source-ID": "fixture-book", "X-Catalog-Revision": str(revision),
               "X-Document-Format": "epub", "X-PDF-Add": "1", "Content-Type": "application/epub+zip",
               "X-PDF-Metadata": quote(json.dumps({"title": "My book"}))}
    request("/api/pdf-upload", data, upload_headers=headers)
    base = "/api/library/epub/fixture-book"
    request(base, status=403)
    request(base + "/document.epub", status=403)
    catalog = request("/api/sources")
    pub = dict(source_id="fixture-book", enabled=True, revision=catalog["revision"])
    request("/api/publication", pub, status=409)
    request("/api/publication", dict(pub, acknowledge_rights=True))
    assert "Open EPUB" in request("/")
    assert "My book" in request("/search?q=My+book")
    page, headers = request(base, raw=True)
    assert b"Safe chapter" in page and b"Table of contents" in page
    assert b"https://example.org" not in page and b"<script>" not in page
    assert b"&lt;script&gt;" in page
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert b'class="book large"' in request(base + "/chapter/0?size=large", raw=True)[0]
    original, headers = request(base + "/document.epub", raw=True)
    assert original == data and headers["Content-Disposition"].startswith("attachment;")
    catalog = request("/api/sources")
    request("/api/publication", dict(pub, enabled=False, revision=catalog["revision"]))
    request(base, status=403)
    request(base + "/document.epub", status=403)
    assert "Open EPUB" not in request("/")
    assert (disk / "library/epub" / source(catalog, "fixture-book")["artifact"]["filename"]).read_bytes() == data


def test_embedded_raster_and_external_resources():
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII=")
    data = book(chapter='<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="cover.png" alt="Cover &amp; art"/><img src="evil.png" alt="Rejected"/><img src="https://external.test/img" alt="External"/><img src="hidden.png" alt="Unlisted"/></body></html>',
                manifest_extra='<item id="i" href="cover.png" media-type="image/png"/><item id="x" href="evil.png" media-type="image/png"/>',
                extra=[("OPS/cover.png", png), ("OPS/evil.png", b"<svg onload='bad()'/>"), ("OPS/hidden.png", png)])
    content, labels = epub_source.chapter_content(data, 0)
    assert content.count("<img ") == 1
    assert 'src="data:image/png;base64,' in content
    assert 'alt="Cover &amp; art"' in content
    assert "Rejected" in content and "External" in content and "Unlisted" in content
    assert "external.test" not in content and "<svg" not in content


def test_image_pixel_budget():
    import struct
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 8192, 8192) + b"\0" * 9
    assert not epub_source._raster_type(header, "image/png")
