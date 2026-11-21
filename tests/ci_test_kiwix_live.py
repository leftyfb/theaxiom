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
         "--urlRootLocation=/zim/", "--monitorLibrary", "--library", str(manifest)],
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
        # Test whether Kiwix's search code requires the optional XML book
        # 'name' attribute; keep the production manifest unchanged until proven.
        diagnostic_root = ET.fromstring(content)
        diagnostic_root.find("book").set("name", "axiom-smoke")
        # Kiwix library IDs are archive UUIDs, not application source IDs.
        # Use the archive's own UUID in this isolated experiment.
        info = subprocess.run(["zimdump", "info", str(archive)],
                              capture_output=True, text=True, timeout=15)
        print("ZIMDUMP INFO:", (info.stdout + info.stderr)[-2000:], flush=True)
        import re
        uuid_match = re.search(r"(?im)^\\s*(?:uuid|id)\\s*[:=]\\s*([a-f0-9-]{32,36})\\s*$", info.stdout)
        if uuid_match:
            diagnostic_root.find("book").set("id", uuid_match.group(1))
        else:
            print("ZIM UUID not parsed; retaining source ID for diagnostic", flush=True)
        content = ET.tostring(diagnostic_root, encoding="utf-8", xml_declaration=True)
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
            # Exercise native search, not only the viewer landing page. A
            # missing search index must not cause an uncaught map::at error.
            from urllib.parse import urlencode
            # Kiwix's search selector is the ZIM metadata Name, not the
            # digest-suffixed filename-derived OPDS content identifier.
            # The fixture deliberately gives them different values to catch
            # mistaken coupling between reader routes and search selection.
            # Capture complete native error semantics for both identifiers.
            # A fixture without a Xapian full-text index may legitimately
            # return 400; record that distinctly from a server exception.
            search_failures = []
            for selector, value in (
                ("books.name", "axiom-smoke"),
                ("books.name", expected),
                ("content", expected),
            ):
                search_path = "/zim/search?" + urlencode(
                    {selector: value, "pattern": "offline"})
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", search_path)
                    response = connection.getresponse()
                    body = response.read(256 * 1024)
                    from html import unescape
                    import re
                    readable = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
                    readable = re.sub(r"\\s+", " ", unescape(readable))
                    print(f"SEARCH DIAGNOSTIC {selector}={value[:28]} "
                          f"HTTP {response.status}: {readable[-900:]}", flush=True)
                    if response.status >= 500 or b"map::at" in body:
                        search_failures.append((selector, response.status))
                finally:
                    connection.close()
            # Probe all selectors even when one fails: the fixture may not
            # contain a full-text index, which must be distinguished from
            # incorrect book selection. Kiwix errors are diagnostic here.
            # The viewer routes by filename-derived content ID. Exercise the
            # search URL that the same identity produces as well as the
            # metadata-name selector, so a passing search smoke test cannot
            # conceal the real-world map::at failure.
            print("ZIMWRITERFS HELP (index options):", flush=True)
            help_result = subprocess.run(["zimwriterfs", "--help"],
                                         capture_output=True, text=True, timeout=10)
            print("\n".join(line for line in
                  (help_result.stdout + help_result.stderr).splitlines()
                  if "index" in line.lower() or "search" in line.lower()), flush=True)
            # Isolate the archive and search engine from library XML loading.
            # The direct-file server must never access production archives.
            with socket.socket() as direct_sock:
                direct_sock.bind(("127.0.0.1", 0))
                direct_port = direct_sock.getsockname()[1]
            direct_process = subprocess.Popen(
                ["kiwix-serve", "--address=127.0.0.1",
                 f"--port={direct_port}", "--urlRootLocation=/zim/", str(archive)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for attempt in range(40):
                    if direct_process.poll() is not None:
                        raise RuntimeError("direct-file Kiwix exited: " + direct_process.stdout.read())
                    try:
                        direct_names = zim_reader_probe.listed_book_ids(port=direct_port)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError("direct-file Kiwix did not start")
                print(f"DIRECT-FILE OPDS: {direct_names!r}", flush=True)
                direct_connection = http.client.HTTPConnection("127.0.0.1", direct_port, timeout=5)
                try:
                    direct_connection.request("GET", "/zim/search?" + urlencode(
                        {"books.name": expected, "pattern": "offline"}))
                    direct_response = direct_connection.getresponse()
                    direct_body = direct_response.read(256 * 1024)
                    print(f"DIRECT-FILE SEARCH HTTP {direct_response.status} "
                          f"map::at={b'map::at' in direct_body}", flush=True)
                finally:
                    direct_connection.close()
            finally:
                direct_process.terminate()
                try:
                    direct_process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    direct_process.kill()
                    direct_process.communicate(timeout=5)
            assert not search_failures, f"native Kiwix search crashes: {search_failures!r}"
            print(f"PASS isolated native Kiwix search diagnostics completed: {expected}")
            print(f"PASS real ZIM OPDS and viewer: {expected}")
            # The live service must pick up a second book without a restart,
            # then remove the first when publication changes.
            next_source = base / "sample-second.zim"
            subprocess.run(
                ["zimwriterfs", "--welcome=index.html", "--illustration=logo.png",
                 "--language=eng", "--name=axiom-second", "--title=Second Fixture",
                 "--description=Another isolated Kiwix smoke fixture",
                 "--creator=Axiom", "--publisher=Axiom",
                 str(pages), str(next_source)],
                check=True, timeout=60,
            )
            second_digest = hashlib.sha256(next_source.read_bytes()).hexdigest()
            second_archive = base / ("second." + second_digest + ".zim")
            shutil.move(next_source, second_archive)
            second = {"id": "second", "path": str(second_archive),
                      "title": "Second Test", "language": "eng"}
            with patch.object(zim_catalog, "verified_books", return_value=[book, second]):
                updated = zim_catalog.render_xml([])
            with (patch.object(zim_catalog, "verified_books", return_value=[book, second]),
                  patch.object(zim_catalog, "manifest_path", return_value=manifest)):
                zim_catalog.write_manifest([])
            second_name = zim_catalog.content_name(second)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if zim_reader_probe.listed_book_ids(port=port) == {expected, second_name}:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Kiwix failed to automatically load the newly published second book")
            with patch.object(zim_catalog, "verified_books", return_value=[second]):
                updated = zim_catalog.render_xml([])
            with (patch.object(zim_catalog, "verified_books", return_value=[second]),
                  patch.object(zim_catalog, "manifest_path", return_value=manifest)):
                zim_catalog.write_manifest([])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if zim_reader_probe.listed_book_ids(port=port) == {second_name}:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Kiwix failed to automatically remove the unpublished first book")
            print("PASS real Kiwix dynamic two-book publish and unpublish reload")

        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    main()
