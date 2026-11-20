"""CI-only small real ZIM / actual kiwix-serve compatibility smoke test.

Runs entirely within a GitHub Actions runner, never against Vancouver. No
downloads beyond Ubuntu packages and no deployment side effects.
"""
import struct
import zlib
import hashlib
import http.client
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch
import xml.etree.ElementTree as ET

from theaxiom import zim_catalog, zim_reader_probe


def serve(port, manifest):
    return subprocess.Popen(
        ["kiwix-serve", "--address=127.0.0.1", f"--port={port}",
         "--urlRootLocation=/zim/", "--library", str(manifest)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="theaxiom-kiwix-smoke-") as root:
        base = Path(root)
        pages = base / "pages"
        pages.mkdir()
        (pages / "index.html").write_text(
            "<!doctype html><title>Offline Ready</title><h1>Ready for offline use</h1>")
        # zimwriterfs requires a proper 48x48 PNG illustration.
        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data +
                    struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
        rows = (b"\x00" + b"\x00\x80\x20" * 48) * 48
        png = (b"\x89PNG\r\n\x1a\n" +
               chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 48, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        (pages / "logo.png").write_bytes(png)
        output = base / "sample.zim"
        subprocess.run(
            ["zimwriterfs", "--welcome=index.html", "--illustration=logo.png",
             "--language=eng", "--name=axiom-smoke", "--title=Offline Fixture",
             "--description=Isolated Kiwix smoke fixture", "--creator=Axiom",
             "--publisher=Axiom", str(pages), str(output)],
            check=True, timeout=60,
        )
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        filename = "ready." + digest + ".zim"
        archive = base / filename
        shutil.move(output, archive)
        book = {"id": "ready", "path": str(archive),
                "title": "Isolated Test", "language": "eng"}
        with patch.object(zim_catalog, "verified_books", return_value=[book]):
            content = zim_catalog.render_xml([])
        manifest = base / "library.xml"
        manifest.write_bytes(content)
        assert ET.fromstring(content).find("book").get("path") == str(archive)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = serve(port, manifest)
        try:
            deadline = time.monotonic() + 20
            last_error = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("kiwix-serve exited: " + process.stdout.read())
                try:
                    names = zim_reader_probe.listed_book_ids(port=port)
                    break
                except (OSError, ValueError) as exc:
                    last_error = exc
                    time.sleep(0.3)
            else:
                raise RuntimeError(f"kiwix-serve failed to become ready: {last_error}")
            expected = zim_catalog.content_name(book)
            assert names == {expected}, f"OPDS {names!r} != filename-derived {expected!r}"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/zim/viewer")
                result = connection.getresponse()
                result.read()
                assert result.status == 200, f"viewer status: {result.status}"
            finally:
                connection.close()
            print(f"PASS real ZIM OPDS and viewer: {expected}")
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    main()
