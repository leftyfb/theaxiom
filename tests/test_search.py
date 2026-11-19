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
