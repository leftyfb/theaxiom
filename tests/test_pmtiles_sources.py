import hashlib
from io import BytesIO
import struct

import pytest
from theaxiom import admin_api, catalog_store, pmtiles_source, zim_jobs, zim_source
from test_source_profiles_http import app, mutate
from test_zim_source import disk


def fixture():
    # Envelope-only fixture, never added to the production source catalog.
    header = bytearray(127)
    header[:8] = b"PMTiles\x03"
    struct.pack_into("<8Q", header, 8, 127, 1, 131, 2, 133, 0, 128, 3)
    header[97:102] = bytes([1, 1, 1, 0, 15])
    return bytes(header) + b"\x00abc{}"


def config(data):
    return dict(format="pmtiles", source_url="https://example.org/map.pmtiles",
                expected_size_bytes=str(len(data)), publisher_md5=hashlib.md5(data, usedforsecurity=False).hexdigest())


def test_pmtiles_checksum_and_header_integrity(disk):
    data = fixture()
    pmtiles_source.check_header(data[:127], len(data))
    artifact = zim_source.install_stream("map", config(data), BytesIO(data))
    assert artifact["filename"].endswith(".pmtiles")
    assert (disk / "library/pmtiles" / artifact["filename"]).read_bytes() == data
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="Publisher MD5"):
        zim_source.install_stream("bad", dict(config(data), publisher_md5="0" * 32), BytesIO(data))
    bad = bytearray(data[:127])
    struct.pack_into("<Q", bad, 8, 1)
    with pytest.raises(ValueError):
        pmtiles_source.check_header(bytes(bad), len(data))


def test_map_publication_range_and_unpublish(app, disk):
    request, _ = app
    data = fixture()
    fields = dict(config(data), title="Map fixture", license="ODbL", search_enabled="no")
    catalog = mutate(request, request("/api/sources"), "add", fields, sid="fixture-map")
    artifact = zim_source.install_stream("fixture-map", fields, BytesIO(data))
    zim_jobs.save(dict(source_id="fixture-map", state="completed", active=False, verified=True,
                       configuration=zim_jobs.identity(fields), artifact=artifact))
    path = "/api/library/pmtiles/fixture-map"
    request(path, status=403)
    request("/api/publication", dict(source_id="fixture-map", enabled=True, revision=catalog["revision"]))
    assert "Map fixture" in request("/")
    assert "Back to library" in request(path)
    body, headers = request(path + "/map.pmtiles", raw=True, status=206, get_headers={"Range": "bytes=0-15"})
    assert body == data[:16]
    assert headers["Content-Range"] == f"bytes 0-15/{len(data)}"
    assert headers["ETag"] == '"' + artifact["sha256"] + '"'
    request(path + "/map.pmtiles", raw=True, status=416)
    request(path + "/map.pmtiles", raw=True, status=412, get_headers={"Range": "bytes=0-15", "If-Match": '"old"'})
    assert 'data-source="fixture-map"' not in request("/search?q=map&mode=progressive")
    revision = request("/api/sources")["revision"]
    request("/api/publication", dict(source_id="fixture-map", enabled=False, revision=revision))
    request(path + "/map.pmtiles", status=403, get_headers={"Range": "bytes=0-15"})


def test_world_seed_upgrade_adds_once_and_preserves_removed_sources(tmp_path):
    import json
    path = tmp_path / "catalog.json"
    store = catalog_store.CatalogStore(path)
    initial = store.get()
    state = json.loads(path.read_text())
    state.pop("seed_version")
    state["sources"] = [item for item in state["sources"] if item["source_id"] not in {"openstreetmap-world", "wikipedia-en"}]
    path.write_text(json.dumps(state))
    upgraded = store.get()
    world = next(item for item in upgraded["sources"] if item["source_id"] == "openstreetmap-world")
    assert world["configuration"]["format"] == "pmtiles"
    assert world["configuration"]["expected_size_bytes"] == "139816078848"
    assert all(item["source_id"] != "wikipedia-en" for item in upgraded["sources"])
    removed = store.mutate("remove", "openstreetmap-world", {}, upgraded["revision"])
    assert all(item["source_id"] != "openstreetmap-world" for item in removed["sources"])
    assert store.get() == removed
    assert initial["revision"] == 0
