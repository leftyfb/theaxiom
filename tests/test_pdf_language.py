import zlib

import pytest

from theaxiom import pdf_language
from test_pdf_sources import pdf_app, disk, SID, source, mutate  # noqa: F401


def pdf(text="", lang=None, compressed=False, operators=None, nested=False, array=False):
    content = operators if operators is not None else ("BT (" + text + ") Tj ET").encode("latin-1")
    if compressed:
        content = zlib.compress(content)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R" + (b" /Lang (" + lang.encode() + b")" if lang else b"") + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]" + (b" /Resources << /Font << /F1 8 0 R >> >>" if nested else b"") + (b" /Contents [4 0 R] >>" if array else b" /Contents 4 0 R >>"),
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


@pytest.mark.parametrize("compressed", [False, True])
def test_nested_resources_array_and_tj_operators(compressed):
    operators = b"BT [(" + EN[:20].encode() + b") -10 (" + EN[20:].encode() + b")] TJ ET"
    assert pdf_language.detect(pdf(operators=operators, nested=True, array=True, compressed=compressed)) == "en"
    assert pdf_language.detect(pdf(nested=True, array=True)) is None


def test_malformed_numeric_array_cannot_stall_parser():
    # The old repeated numeric alternation could repartition this digit run
    # exponentially when the final token was invalid.
    operators = b"BT [" + b"1" * 100 + b" X] TJ (" + EN.encode() + b") Tj ET"
    assert pdf_language.detect(pdf(operators=operators)) == "en"


def test_detection_timeout_does_not_prevent_upload(pdf_app, disk, monkeypatch):
    import json
    import subprocess
    from urllib.parse import quote

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == pdf_language.DETECTION_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(pdf_language.subprocess, "run", timeout)
    request, _ = pdf_app
    revision = request("/api/sources")["revision"]
    headers = {"X-Source-ID": SID, "X-Catalog-Revision": str(revision), "X-PDF-Add": "1",
               "X-PDF-Metadata": quote(json.dumps({"title": "Guide"}))}
    assert request("/api/pdf-upload", pdf(EN), upload_headers=headers)["installed_verified"]
    assert not source(request("/api/sources"), SID)["profile"].get("language")


def test_real_worker_deadline(monkeypatch):
    # A real subprocess must be killed and reaped, not merely a timed-out thread.
    import sys
    import time
    original = pdf_language.subprocess.run

    def slow_worker(args, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(pdf_language.subprocess, "run", slow_worker)
    monkeypatch.setattr(pdf_language, "DETECTION_TIMEOUT", 0.25)
    start = time.monotonic()
    assert pdf_language.detect(pdf(EN)) is None
    assert time.monotonic() - start < 5
