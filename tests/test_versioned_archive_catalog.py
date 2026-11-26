import json
import pytest
from theaxiom import versioned_archive_catalog as catalog


def encoded(value):
    return json.dumps(value).encode()


def test_archive_edition_and_digest_are_exact():
    item = "example-map"
    def entry(name, source="original"):
        return dict(name=name, size="12345", sha1="a" * 40, source=source)
    body = dict(metadata=dict(identifier=item), files=[entry("map-2026-06-01.zim"),
        entry("map-2026-06-01b.zim"), entry("map-2026-07-01.zim", "derivative"),
        entry("other-2026-08-01.zim")])
    kwargs = dict(endpoint="https://archive.org/metadata/example-map", family="map-")
    result = catalog.internet_archive_latest(encoded(body), **kwargs)
    assert result == dict(filename="map-2026-06-01b.zim", edition="2026-06-01b", bytes=12345,
        source_url="https://archive.org/download/example-map/map-2026-06-01b.zim",
        digest_algorithm="sha1", digest="a" * 40)
    body["files"].append(body["files"][1])
    with pytest.raises(ValueError, match="Ambiguous"):
        catalog.internet_archive_latest(encoded(body), **kwargs)


def test_protomaps_only_matching_major_and_complete_planet():
    def entry(key, version="4.15.2"):
        return dict(key=key, version=version, size=139816078848, b3sum="b" * 64)
    body = [entry("20261125.pmtiles"), entry("20261126.pmtiles"),
            entry("20261127.pmtiles", "5.0.0"), entry("europe-20261128.pmtiles")]
    result = catalog.protomaps_latest(encoded(body))
    assert result["edition"] == "2026-11-26"
    assert result["digest_algorithm"] == "blake3"
    assert result["bytes"] == 139816078848
    assert result["source_url"] == "https://build.protomaps.com/20261126.pmtiles"
    body[1]["b3sum"] = "invalid"
    with pytest.raises(ValueError, match="evidence"):
        catalog.protomaps_latest(encoded(body))


def test_bad_metadata_and_mismatched_item_fail_closed():
    with pytest.raises(ValueError):
        catalog.internet_archive_latest(encoded(dict(metadata=dict(identifier="wrong"), files=[])),
            endpoint="https://archive.org/metadata/right", family="map-")
    with pytest.raises(ValueError):
        catalog.protomaps_latest(b"x" * (catalog.MAX_METADATA_BYTES + 1))


def test_archive_check_distinguishes_matching_newer_and_changed_editions():
    from theaxiom import update_discovery_config, admin_api
    url = "https://archive.org/download/example-map/map-2026-06-01.zim"
    config = dict(format="zim", source_url=url, publisher_sha1="a" * 40)
    settings = update_discovery_config.settings(config)
    assert settings == dict(method="archive", endpoint="https://archive.org/metadata/example-map", periodic=True)
    installed = dict(verified=True, source_url=url, publisher_sha1="a" * 40)
    def check(name, digest):
        return catalog.check_archive_update(installed, fetch=lambda endpoint: encoded(dict(
            metadata=dict(identifier="example-map"), files=[dict(name=name, size="12345", sha1=digest, source="original")])))
    assert check("map-2026-06-01.zim", "a" * 40)["state"] == "up_to_date"
    assert check("map-2026-06-01.zim", "b" * 40)["state"] == "unable_to_determine"
    update = check("map-2026-07-01.zim", "b" * 40)
    assert update["state"] == "update_available"
    assert admin_api._reviewed_update_fields(update, "b" * 40)["publisher_sha1"] == "b" * 40
    assert admin_api._reviewed_update_fields(update, "b" * 40)["sha256"] == ""
    with pytest.raises(ValueError):
        admin_api._reviewed_update_fields(update, "c" * 40)


def test_official_streetzim_seed_is_configured_without_extra_map_sources(tmp_path):
    from theaxiom import catalog_store
    records = catalog_store.CatalogStore(tmp_path / "catalog.json").get()["sources"]
    record = next(record for record in records if record["source_id"] == "openstreetmap")
    assert record["profile"]["title"] == "OpenStreetMap — United States"
    config = record["configuration"]
    assert config["format"] == "zim" and config["update_method"] == "archive"
    assert config["search_enabled"] == "no"
    assert config["expected_size_bytes"] == "34883793295"
    assert "noncommercial" in config["license"]


def test_protomaps_update_uses_actual_publisher_md5_not_a_fake_sha256():
    import base64
    from theaxiom import update_discovery_config, admin_api
    url = "https://build.protomaps.com/20261125.pmtiles"
    settings = update_discovery_config.settings(dict(format="pmtiles", source_url=url))
    assert settings == dict(method="builds", endpoint=catalog.PROTOMAPS_ENDPOINT, periodic=True)
    installed = dict(verified=True, source_url=url, publisher_md5="a" * 32)
    def check(day, md5):
        return catalog.check_protomaps_update(installed, fetch=lambda endpoint: encoded([
            dict(key=day + ".pmtiles", version="4.15.2", size=139816078848,
                 b3sum="b" * 64, md5sum=base64.b64encode(bytes.fromhex(md5)).decode())]))
    assert check("20261125", "a" * 32)["state"] == "up_to_date"
    update = check("20261126", "c" * 32)
    assert update["state"] == "update_available"
    assert admin_api._reviewed_update_fields(update, "c" * 32)["publisher_md5"] == "c" * 32
    assert admin_api._reviewed_update_fields(update, "c" * 32)["sha256"] == ""
