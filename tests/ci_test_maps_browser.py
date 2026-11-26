"""Real offline MapLibre rendering with synthetic PMTiles, never production data."""
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from theaxiom import admin_api, catalog_store, pdf_source, publication, storage, storage_settings, zim_jobs, zim_source


def tiny_pmtiles():
    # One z0 vector tile with an empty but valid earth layer; no real map data.
    layer = b'\x78\x02\x0a\x05earth\x28\x80\x20'
    tile = bytes((26, len(layer))) + layer
    root = bytes((1, 0, 1, len(tile), 1))
    metadata = json.dumps({"vector_layers": [{"id": "earth", "fields": {}}]}).encode()
    meta_offset = 127 + len(root)
    tile_offset = meta_offset + len(metadata)
    header = bytearray(127)
    header[:8] = b"PMTiles\x03"
    struct.pack_into("<11Q", header, 8, 127, len(root), meta_offset, len(metadata), tile_offset, 0,
                     tile_offset, len(tile), 1, 1, 1)
    header[96:102] = bytes((1, 1, 1, 1, 0, 0))
    struct.pack_into("<4i", header, 102, -1800000000, -850000000, 1800000000, 850000000)
    return bytes(header) + root + metadata + tile


def main():
    with tempfile.TemporaryDirectory(prefix="axiom-map-regression-") as tmp, ExitStack() as stack:
        root = Path(tmp)
        disk = root / "disk"
        for branch in ("library", "staging"):
            (disk / branch).mkdir(parents=True)
        stack.enter_context(patch.object(storage_settings, "SETTINGS_PATH", root / "settings.json"))
        mount = storage._covering(disk, storage.read_mounts())
        stack.enter_context(patch.object(pdf_source, "LOCAL_FILESYSTEMS", {mount["type"]}))
        storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(disk)))
        catalog = catalog_store.CatalogStore(root / "sources.json")
        stack.enter_context(patch.object(admin_api, "SOURCE_CATALOG", catalog))
        data = tiny_pmtiles()
        config = dict(format="pmtiles", source_url="https://example.org/fixture.pmtiles",
                      expected_size_bytes=str(len(data)), publisher_md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
                      title="Fixture map", license="Synthetic test data", search_enabled="no")
        sid = "reader-map"
        catalog.mutate("add", sid, config, catalog.get()["revision"])
        artifact = zim_source.install_stream(sid, config, BytesIO(data))
        zim_jobs.save(dict(source_id=sid, state="completed", active=False, verified=True,
                           configuration=zim_jobs.identity(config), artifact=artifact))
        publication.save(sid, True)
        server = admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler)
        server.acquisition_monitor = SimpleNamespace(snapshot=lambda: {"stale": False, "acquisitions": []})
        host, port = server.server_address
        base = f"http://{host}:{port}"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as playwright:
                options = {"headless": True, "args": ["--enable-unsafe-swiftshader"]}
                if os.environ.get("AXIOM_CHROMIUM_EXECUTABLE"):
                    options["executable_path"] = os.environ["AXIOM_CHROMIUM_EXECUTABLE"]
                browser = playwright.chromium.launch(**options)
                try:
                    for width, height in ((1366, 900), (390, 844)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        errors, external, responses = [], [], []
                        page.on("pageerror", lambda e: errors.append(str(e)))
                        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                        page.on("response", lambda r: responses.append((r.url, r.status)))
                        def route(intercept):
                            if not intercept.request.url.startswith(base + "/"):
                                external.append(intercept.request.url)
                                intercept.abort()
                            else:
                                intercept.continue_()
                        page.route("**/*", route)
                        page.goto(base + "/api/library/pmtiles/reader-map")
                        try:
                            expect(page.locator("#map")).to_have_attribute("data-ready", "true", timeout=25000)
                            expect(page.locator("#map-error")).to_be_hidden()
                            expect(page.locator("canvas.maplibregl-canvas")).to_be_visible()
                            page.get_by_role("button", name="Zoom in").click()
                            expect(page.get_by_role("link", name="Back to library")).to_be_visible()
                            assert not external, external
                            assert not errors, errors
                            assert any(url.endswith("map.pmtiles") and status == 206 for url, status in responses)
                            page.get_by_role("link", name="Back to library").click()
                            expect(page).to_have_url(base + "/")
                        except Exception:
                            print("Map errors:", errors, "HTTP:", responses, "external:", external)
                            raise
                        finally:
                            page.close()
                    print("PASS desktop and mobile offline MapLibre map rendering, ranges and zoom")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
