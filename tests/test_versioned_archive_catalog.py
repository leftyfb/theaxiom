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
