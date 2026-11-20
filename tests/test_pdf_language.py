import zlib

import pytest

from theaxiom import pdf_language
from test_pdf_sources import pdf_app, disk, SID, source, mutate  # noqa: F401


def pdf(text="", lang=None, compressed=False):
    content = ("BT (" + text + ") Tj ET").encode("latin-1")
    if compressed:
        content = zlib.compress(content)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R" + (b" /Lang (" + lang.encode() + b")" if lang else b"") + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + (b" /Filter /FlateDecode" if compressed else b"") + b" >>\nstream\n" + content + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets)
    return data + f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


EN = "the water and the food in your home should be safe for you and your family and this is the way that you can prepare for an emergency with the supplies that you have " * 2
ES = "el agua y los alimentos de la casa que se guardan para una emergencia con la familia son de gran importancia por esta razón se debe mantener una reserva en su casa " * 2


@pytest.mark.parametrize("compressed", [False, True])
def test_text_sampling(compressed):
    assert pdf_language.detect(pdf(EN, compressed=compressed)) == "en"
    assert pdf_language.detect(pdf(ES, compressed=compressed)) == "es"


def test_metadata_and_abstention():
    assert pdf_language.detect(pdf(lang="fr-CA")) == "fr-ca"
    assert pdf_language.detect(pdf(EN, lang="es")) is None
    assert pdf_language.detect(pdf()) is None
    assert pdf_language.detect(pdf("hello world")) is None
    assert pdf_language.detect(pdf(EN + ES)) is None
    assert pdf_language.detect(b"invalid") is None


@pytest.mark.parametrize("override", ["", "German"])
def test_upload_preserves_manual_language(pdf_app, disk, override):
    import json
    from urllib.parse import quote
    request, _ = pdf_app
    revision = request("/api/sources")["revision"]
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(revision), "X-PDF-Add": "1",
               "X-PDF-Metadata": quote(json.dumps({"title": "Guide", "language": override}))}
    assert request("/api/pdf-upload", pdf(EN), upload_headers=headers)["installed_verified"]
    assert source(request("/api/sources"), SID)["profile"]["language"] == (override or "en")
