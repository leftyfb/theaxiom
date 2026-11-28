"""Temporary one-time publisher logo retrieval; never a deployment gate."""
import base64
import os
import struct
import subprocess
import sys
import urllib.request

import pytest


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true", reason="one-time asset retrieval")
def test_retrieve_official_wikimed_logo(capsys):
    url = "https://upload.wikimedia.org/wikipedia/commons/5/58/Wiki_Project_Med_Foundation_logo.svg"
    request = urllib.request.Request(url, headers={"User-Agent": "TheAxiom/0.1 (https://github.com/leftyfb/theaxiom; one-time logo asset retrieval)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        svg = response.read(65537)
    assert 100 < len(svg) <= 65536 and b"<svg" in svg
    subprocess.run([sys.executable, "-m", "pip", "install", "cairosvg"], check=True, timeout=90)
    import cairosvg
    raw = cairosvg.svg2png(bytestring=svg, output_width=24, output_height=26)
    encoded = base64.b64encode(raw).decode("ascii")
    assert raw[:8] == bytes.fromhex("89504e470d0a1a0a")
    assert struct.unpack(">II", raw[16:24]) == (24, 26)
    assert len(encoded) <= 4096
    with capsys.disabled():
        print("ISSUE56_OFFICIAL_LOGO_PNG=" + encoded, flush=True)
    pytest.exit("One-time asset retrieval complete; this temporary revision must not be deployed.", returncode=2)
