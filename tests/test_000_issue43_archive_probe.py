"""Temporary bounded public-domain starter-document verification (not a deployable revision)."""
import hashlib
import io
import json
import os
import re
import urllib.request

import pytest

from theaxiom import document_search, pdf_source, versioned_archive_catalog


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true", reason="one-time upstream verification")
def test_verify_archive_reference(capsys):
    item = "farmmechanicsdra00newy"
    endpoint = "https://archive.org/metadata/" + item
    body = json.loads(versioned_archive_catalog.fetch_metadata(endpoint))
    meta = body["metadata"]
    assert meta["identifier"] == item and str(meta["date"]).startswith("1911")
    assert "library of congress" in meta["possible-copyright-status"].lower()
    assert not meta.get("access-restricted-item")
    entries = [f for f in body["files"] if f.get("name") == item + ".pdf"]
    assert len(entries) == 1
    f = entries[0]
    assert not f.get("private") and re.fullmatch(r"[a-f0-9]{40}", f["sha1"])
    size = int(f["size"])
    assert 0 < size <= pdf_source.MAX_BYTES
    url = "https://archive.org/download/" + item + "/" + f["name"]
    request = urllib.request.Request(url, headers={"User-Agent": "TheAxiom/0.1 (https://github.com/leftyfb/theaxiom; one-time public-domain reference validation)"})
    with urllib.request.urlopen(request, timeout=35) as response:
        raw = response.read(size + 1)
    assert len(raw) == size and hashlib.sha1(raw).hexdigest() == f["sha1"]
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    assert not reader.is_encrypted
    sections, partial = document_search._sections(raw, "pdf")
    assert sections
    hits = [(n, len(t)) for n, t in sections if "drawing" in t.lower()]
    assert hits
    images = sum(bool(p.images) for p in reader.pages)
    assert images
    with capsys.disabled():
        print("ISSUE43_REFERENCE=" + json.dumps(dict(identifier=item, size=size, sha1=f["sha1"], sha256=hashlib.sha256(raw).hexdigest(), format=f.get("format"), mtime=f.get("mtime"), pages=len(reader.pages), text_pages=len(sections), image_pages=images, partial=partial, drawing_hits=hits[:5], copyright_status=meta["possible-copyright-status"])), flush=True)
    pytest.exit("One-time reference verification complete; do not deploy this temporary revision.", returncode=2)
