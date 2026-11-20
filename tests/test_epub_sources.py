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
