"""Temporary one-time retrieval of the publisher logo; removed before deployment."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import pytest


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true", reason="one-time logo asset retrieval")
def test_retrieve_official_wikimed_logo(capsys):
    url = "https://upload.wikimedia.org/wikipedia/commons/5/58/Wiki_Project_Med_Foundation_logo.svg"
    request = urllib.request.Request(url, headers={"User-Agent": "TheAxiom/0.1 (https://github.com/leftyfb/theaxiom; one-time logo asset retrieval)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        svg = response.read(65537)
    assert 100 < len(svg) <= 65536 and b"<svg" in svg
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "logo.svg"
        target = Path(tmp) / "logo.png"
        source.write_bytes(svg)
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-w", "24", "-h", "26", "-o", str(target), str(source)], check=True, timeout=20)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "cairosvg"], check=True, timeout=90)
            subprocess.run([sys.executable, "-m", "cairosvg", "-W", "24", "-H", "26", "-o", str(target), str(source)], check=True, timeout=20)
        raw = target.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        assert raw.startswith(b"\\x89PNG\\r\\n\\x1a\\n") and len(encoded) <= 4096
        with capsys.disabled():
            print("\nISSUE56_OFFICIAL_LOGO_PNG=" + encoded, flush=True)
