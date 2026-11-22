"""CI-only small real ZIM / actual kiwix-serve compatibility smoke test.

Runs entirely within a GitHub Actions runner, never against Vancouver. No
downloads beyond Ubuntu packages and no deployment side effects.
"""
import struct
import zlib
import hashlib
import http.client
from io import BytesIO
from contextlib import contextmanager
import threading
import pytest
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch
import xml.etree.ElementTree as ET

from theaxiom import zim_catalog, zim_reader_probe, zim_source, zim_jobs, storage_settings


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
            '<!doctype html><title>Offline Ready</title><h1>Ready for offline use</h1>'
            '<a href="second.html">Next offline topic</a>')
        (pages / "second.html").write_text(
            '<!doctype html><title>Second topic</title><h1>Second offline article</h1>'
            '<a href="index.html">Back to first article</a>')
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
        # Feed a genuine zimwriterfs archive through Axiom's verification and
        # atomic publication path before asking kiwix-serve to read it.
        payload = output.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        storage_root = base / "storage"
        storage_root.mkdir()
        (storage_root / "staging").mkdir()
        (storage_root / "library").mkdir()
        config = {"format": "zim", "source_url": "https://example.org/fixture.zim",
                  "expected_size_bytes": str(len(payload)), "sha256": digest}
        with (patch.object(storage_settings, "SETTINGS_PATH", base / "settings.json"),
              patch.object(zim_source.pdf_source, "LOCAL_FILESYSTEMS",
                           zim_source.pdf_source.LOCAL_FILESYSTEMS | {"overlay"})):
            storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS,
                                                storage_root=str(storage_root)))
            artifact = zim_source.install_stream("ready", config, BytesIO(payload))
            archive = storage_root / "library" / "zim" / artifact["filename"]
            assert archive.read_bytes() == payload
            assert artifact["sha256"] == digest

            # Real-archive lifecycle: start with retained bytes, require an
            # explicit resume and stable publisher validator, then run the
            # actual resumable worker and atomic promotion.
            offset = min(1024, len(payload) // 2)
            stage = storage_root / "staging" / "zim"
            partial = stage / ".download-resumed.partial"
            partial.write_bytes(payload[:offset])
            partial.chmod(0o600)
            zim_jobs.save(dict(source_id="resumed", state="paused", active=False,
                               configuration=zim_jobs.identity(config),
                               downloaded_bytes=offset, expected_bytes=len(payload),
                               etag='"real-zim-v1"'))

            class Body(BytesIO):
                total_size = len(payload)
                etag = '"real-zim-v1"'
                last_modified = None

            calls = []

            @contextmanager
            def transport(url, *, offset, validator, **kwargs):
                calls.append((offset, validator))
                yield Body(payload[offset:])

            with patch.object(zim_source.pdf_transport, "open_pdf", transport):
                assert zim_jobs.start("resumed", config, resume=True)["accepted"]
                for worker in threading.enumerate():
                    if worker.name == "zim-acquisition":
                        worker.join(10)
                        assert not worker.is_alive()
            resumed = zim_jobs.status("resumed")
            assert resumed["state"] == "completed" and resumed["verified"], resumed
            assert calls == [(offset, '"real-zim-v1"')]
            assert not partial.exists()
            assert (storage_root / "library" / "zim" /
                    resumed["artifact"]["filename"]).read_bytes() == payload

            # A real archive must also survive explicit cancel-with-retention,
            # without publishing incomplete bytes or losing its resume identity.
            retained = stage / ".download-retained.partial"
            retained.write_bytes(payload[:offset])
            retained.chmod(0o600)
            zim_jobs.save(dict(source_id="retained", state="failed", active=False,
                               configuration=zim_jobs.identity(config),
                               downloaded_bytes=offset, expected_bytes=len(payload),
                               etag='"real-zim-v1"'))
            assert zim_jobs.control("retained", config, "cancel-keep")["accepted"]
            assert retained.read_bytes() == payload[:offset]
            assert zim_jobs.status("retained")["state"] == "paused"
            assert not list((storage_root / "library" / "zim").glob("retained.*.zim"))
            with patch.object(zim_source.pdf_transport, "open_pdf", transport):
                assert zim_jobs.start("retained", config, resume=True)["accepted"]
                for worker in threading.enumerate():
                    if worker.name == "zim-acquisition":
                        worker.join(10)
                        assert not worker.is_alive()
            recovered = zim_jobs.status("retained")
            assert recovered["state"] == "completed" and recovered["verified"], recovered
            assert not retained.exists()
            assert (storage_root / "library" / "zim" /
                    recovered["artifact"]["filename"]).read_bytes() == payload
            assert calls[-1] == (offset, '"real-zim-v1"')

            # Discard cancellation is destructive only for the selected partial;
            # it must never disturb previously published readable archives.
            discarded = stage / ".download-discarded.partial"
            discarded.write_bytes(payload[:offset])
            discarded.chmod(0o600)
            zim_jobs.save(dict(source_id="discarded", state="paused", active=False,
                               configuration=zim_jobs.identity(config),
                               downloaded_bytes=offset, expected_bytes=len(payload),
                               etag='"real-zim-v1"'))
            assert zim_jobs.control("discarded", config, "cancel")["accepted"]
            assert zim_jobs.status("discarded")["state"] == "cancelled"
            assert not discarded.exists()
            assert not list((storage_root / "library" / "zim").glob("discarded.*.zim"))
            assert archive.read_bytes() == payload
            with pytest.raises(ValueError, match="matching paused"):
                zim_jobs.start("discarded", config, resume=True)

            # A stale active state left by a crashed process must reconcile to
            # paused and retain its actual bytes, not trust the stale counter.
            interrupted = stage / ".download-interrupted.partial"
            interrupted.write_bytes(payload[:offset])
            interrupted.chmod(0o600)
            zim_jobs.save(dict(source_id="interrupted", state="downloading", active=True,
                               configuration=zim_jobs.identity(config),
                               downloaded_bytes=offset + 17, expected_bytes=len(payload),
                               etag='"real-zim-v1"'))
            orphan = zim_jobs.status("interrupted")
            assert orphan["state"] == "paused" and not orphan["active"]
            assert orphan["downloaded_bytes"] == offset
            assert interrupted.read_bytes() == payload[:offset]
            with patch.object(zim_source.pdf_transport, "open_pdf", transport):
                assert zim_jobs.start("interrupted", config, resume=True)["accepted"]
                for worker in threading.enumerate():
                    if worker.name == "zim-acquisition":
                        worker.join(10)
                        assert not worker.is_alive()
            finished = zim_jobs.status("interrupted")
            assert finished["state"] == "completed" and finished["verified"], finished
            assert calls[-1] == (offset, '"real-zim-v1"')
            assert not interrupted.exists()
            assert (storage_root / "library" / "zim" /
                    finished["artifact"]["filename"]).read_bytes() == payload
        output.unlink()
        with archive.open("rb") as stream:
            book_id = zim_catalog.archive_uuid(stream)
        book = {"id": book_id, "path": str(archive),
                "title": "Isolated Test", "language": "eng"}
        with patch.object(zim_catalog, "verified_books", return_value=[book]):
            content = zim_catalog.render_xml([])
        manifest = base / "library.xml"
        manifest.write_bytes(content)
        assert ET.fromstring(content).find("book").get("path") == str(archive)
        with archive.open("rb") as stream:
            assert ET.fromstring(content).find("book").get("id") == zim_catalog.archive_uuid(stream)
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
            selected_search_status = None
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
                    if selector == "books.name" and value == expected:
                        selected_search_status = response.status
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
            assert selected_search_status == 200, f"native library search HTTP {selected_search_status}"
            # Real Chromium follows articles in a tiny fixture, not production
            # Wikipedia/Ready.gov. Search is tested via native Kiwix HTTP as above.
            from playwright.sync_api import sync_playwright, expect
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for label, viewport in (
                        ("desktop", {"width": 1366, "height": 900}),
                        ("mobile", {"width": 390, "height": 844}),
                    ):
                        context = browser.new_context(viewport=viewport)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        try:
                            origin = f"http://127.0.0.1:{port}"
                            start = origin + f"/zim/content/{expected}/index.html"
                            response = page.goto(start)
                            assert response.status == 200, (label, response.status)
                            expect(page.get_by_role("heading", name="Ready for offline use")).to_be_visible()
                            next_link = page.get_by_role("link", name="Next offline topic")
                            next_link.focus()
                            expect(next_link).to_be_focused()
                            page.keyboard.press("Enter")
                            expect(page.get_by_role("heading", name="Second offline article")).to_be_visible()
                            # Browser history is part of the offline reader
                            # journey, including keyboard-only navigation.
                            page.go_back()
                            expect(page.get_by_role("heading", name="Ready for offline use")).to_be_visible()
                            next_link = page.get_by_role("link", name="Next offline topic")
                            expect(next_link).to_be_visible()
                            next_link.click()
                            expect(page.get_by_role("heading", name="Second offline article")).to_be_visible()
                            page.go_back()
                            expect(page.get_by_role("heading", name="Ready for offline use")).to_be_visible()
                            # Exercise the actual native search response in Chromium,
                            # not solely through an HTTP client. The fixture is
                            # indexed by its metadata name, not the OPDS slug.
                            search_url = origin + "/zim/search?" + urlencode({
                                "books.name": expected, "pattern": "offline"})
                            search_response = page.goto(search_url)
                            assert search_response.status == 200, (label, search_response.status)
                            assert "map::at" not in page.locator("body").inner_text(), label
                            page.go_back()
                            expect(page.get_by_role("heading", name="Ready for offline use")).to_be_visible()
                            viewport_width = page.evaluate("document.documentElement.clientWidth")
                            content_width = page.evaluate("document.documentElement.scrollWidth")
                            assert content_width <= viewport_width + 2, (label, content_width, viewport_width)
                            print(f"PASS {label}: native ZIM browser article navigation and back")
                        finally:
                            context.close()
                finally:
                    browser.close()
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
            # OPDS publication must expose a usable article in Chromium.
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    response = page.goto(
                        f"http://127.0.0.1:{port}/zim/content/{second_name}/index.html")
                    assert response.status == 200, ("new book article", response.status)
                    expect(page.get_by_role(
                        "heading", name="Ready for offline use")).to_be_visible()
                    page.get_by_role("link", name="Next offline topic").click()
                    expect(page.get_by_role(
                        "heading", name="Second offline article")).to_be_visible()
                finally:
                    browser.close()
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
            # The old article URL must no longer expose unpublished content.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", f"/zim/content/{expected}/index.html")
                retired = connection.getresponse()
                retired.read()
                assert retired.status in (404, 410), (
                    "unpublished ZIM article remains accessible", retired.status)
            finally:
                connection.close()
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
