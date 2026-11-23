from unittest.mock import patch
import pytest

from theaxiom import search

ROUTE = {"name": "Fixture", "path": "/fixture/"}


def test_query_validation():
    assert search.validate_query(" water ") == "water"
    for query in ("a" * 161, "bad\nquery", "bad\x00query"):
        with pytest.raises(ValueError):
            search.validate_query(query)


@pytest.mark.parametrize("href", [
    "https://example.org/fixture/a", "//example.org/fixture/a",
    "/fixture/../admin", "/fixture/%2e%2e/admin",
    "/fixture/%252e%252e/admin", "/fixture/%5cadmin", "/admin",
])
def test_invalid_links(href):
    assert not search.valid_link(href, ROUTE)


def test_search_only_published_and_bounded():
    def adapter(route, query):
        assert query == "water"
        return [search.Result("Water", "/fixture/article")] * 30
    with patch.dict(search.publication.ROUTES, {"fixture": ROUTE}, clear=True), \
         patch.dict(search.ADAPTERS, {"fixture": search.Adapter("full text", adapter)}, clear=True), \
         patch.object(search.publication, "allowed", return_value=True):
        groups = search.search("water", lambda: {})
        assert len(groups[0]["results"]) == search.MAX_RESULTS
    with patch.dict(search.publication.ROUTES, {"fixture": ROUTE}, clear=True), \
         patch.dict(search.ADAPTERS, {"fixture": search.Adapter("full text", adapter)}, clear=True), \
         patch.object(search.publication, "allowed", side_effect=[True, False]):
        assert search.search("water", lambda: {}) == []


def test_failed_adapter_does_not_expose_results():
    def unavailable(route, query):
        raise OSError("fixture failure")
    with patch.dict(search.publication.ROUTES, {"fixture": ROUTE}, clear=True), \
         patch.dict(search.ADAPTERS, {"fixture": search.Adapter("full text", unavailable)}, clear=True), \
         patch.object(search.publication, "allowed", return_value=True):
        group = search.search("water", lambda: {})[0]
        assert group["status"] == "unavailable"
        assert group["results"] == []


def test_kiwix_rss_scopes_links_and_strips_snippet_markup():
    route = {"path": "/zim/content/approved/"}
    items = (
        '<item><title>Water &amp; health</title><link>/zim/content/approved/Water%20quality</link>'
        '<description><![CDATA[Drink <b>safe</b> water.]]></description></item>'
        '<item><title>Other collection</title><link>/zim/content/other/private</link></item>'
        '<item><title>External</title><link>https://other.org/zim/content/approved/Water</link></item>'
        '<item><title>Traversal</title><link>/zim/content/approved/%2e%2e/private</link></item>'
        '<item><title>Query</title><link>/zim/content/approved/Water?redirect=1</link></item>'
    )
    assert search.parse_kiwix_results(f"<rss><channel>{items}</channel></rss>".encode(), route) == [
        search.Result("Water & health", "/zim/content/approved/Water%20quality", "Drink safe water.")
    ]
    for data in (b"<html/>", b"<!DOCTYPE rss><rss><channel/></rss>", b"x" * (search.MAX_RSS_BYTES + 1)):
        with pytest.raises(ValueError):
            search.parse_kiwix_results(data, route)


def test_grouped_zim_search_rechecks_all_publication_after_adapters_finish():
    published = {"first", "second"}

    def adapter(route, query):
        if route["name"] == "Second":
            published.remove("first")
        return [search.Result("Water", route["path"] + "article")]

    targets = {
        sid: ({"name": sid.title(), "path": "/zim/content/" + sid + "/"},
              search.Adapter("full text", adapter))
        for sid in ("first", "second")
    }
    results = search.search("water", lambda: {}, targets=targets,
                            allowed=lambda sid, route: sid in published)
    assert [group["source_id"] for group in results] == ["second"]
    assert results[0]["results"][0].href == "/zim/content/second/article"


def test_document_result_links_cannot_cross_source_boundaries():
    pdf = {"kind": "pdf", "path": "/api/library/pdf/guide"}
    epub = {"kind": "epub", "path": "/api/library/epub/book"}
    assert search.valid_link(pdf["path"] + "#page=2", pdf)
    assert search.valid_link(epub["path"] + "/chapter/1", epub)
    assert not search.valid_link(pdf["path"] + "-private#page=2", pdf)
    assert not search.valid_link(epub["path"] + "-private/chapter/1", epub)
    assert not search.valid_link(epub["path"] + "/chapter/1?url=other", epub)


def test_document_index_matches_text_and_rejects_stale_digest(tmp_path, monkeypatch):
    import json
    from theaxiom import document_search, storage_settings
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    artifact = {"filename": "guide." + "a" * 64 + ".pdf", "sha256": "a" * 64, "bytes": 100}
    path = document_search._path(artifact)
    path.parent.mkdir()
    path.write_text(json.dumps({"artifact": [artifact["filename"], artifact["sha256"], 100],
                                "sections": [[3, "Safe drinking water"]], "partial": False}))
    path.chmod(0o600)
    route = dict(name="Guide", kind="pdf", path="/api/library/pdf/guide", artifact=artifact)
    assert document_search.search_document(route, "water") == (
        [search.Result("Guide · Page 3", "/api/library/pdf/guide#page=3", "Safe drinking water")], False)
    assert document_search._load(dict(artifact, sha256="b" * 64)) is None
