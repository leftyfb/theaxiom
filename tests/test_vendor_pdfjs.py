import hashlib
import io
import zipfile

import pytest

import vendor_pdfjs


def archive(extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name in sorted(vendor_pdfjs.REQUIRED):
            z.writestr(name, "fixture")
        for name in extra:
            z.writestr(name, "fixture")
    return output.getvalue()


def pin(monkeypatch, data):
    monkeypatch.setattr(vendor_pdfjs, "SIZE", len(data))
    monkeypatch.setattr(vendor_pdfjs, "SHA256", hashlib.sha256(data).hexdigest())


def test_verified_distribution(tmp_path, monkeypatch):
    data = archive()
    pin(monkeypatch, data)
    dest = tmp_path / "pdfjs"
    vendor_pdfjs.extract(data, dest)
    assert (dest / "web/viewer.html").read_text() == "fixture"


def test_wrong_digest(tmp_path, monkeypatch):
    data = archive()
    pin(monkeypatch, data)
    with pytest.raises(ValueError, match="integrity"):
        vendor_pdfjs.extract(data[:-1] + bytes([data[-1] ^ 1]), tmp_path / "pdfjs")
    assert not (tmp_path / "pdfjs").exists()


@pytest.mark.parametrize("name", ["../escaped", "/absolute", "web/../../escaped"])
def test_unsafe_archive(tmp_path, monkeypatch, name):
    data = archive([name])
    pin(monkeypatch, data)
    with pytest.raises(ValueError, match="Unsafe"):
        vendor_pdfjs.extract(data, tmp_path / "pdfjs")
    assert not (tmp_path / "pdfjs").exists()
