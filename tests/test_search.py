from urllib.parse import parse_qs, urlsplit

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
    targets = {"fixture": (ROUTE, search.Adapter("full text", adapter))}
    groups = search.search("water", dict, targets=targets, allowed=lambda *_: True)
    assert len(groups[0]["results"]) == search.MAX_RESULTS
    allowed = iter([True, False])
    assert search.search("water", dict, targets=targets, allowed=lambda *_: next(allowed)) == []


def test_failed_adapter_does_not_expose_results():
    def unavailable(route, query):
        raise OSError("fixture failure")
    targets = {"fixture": (ROUTE, search.Adapter("full text", unavailable))}
    group = search.search("water", dict, targets=targets, allowed=lambda *_: True)[0]
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
    assert search.parse_kiwix_results(f"<rss><channel>{items}</channel></rss>".encode(), route).results == [
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
    assert document_search.search_document(route, "water") == search.Page(
        [search.Result("Guide · Page 3", "/api/library/pdf/guide#page=3", "Safe drinking water")])
    assert document_search._load(dict(artifact, sha256="b" * 64)) is None


@pytest.mark.parametrize("query", ["water", "food", "clothes", "cambridge", "  café & tea?  "])
@pytest.mark.parametrize("base", [0, 1])
def test_kiwix_search_pages_and_escapes_query(query, base, monkeypatch):
    from io import BytesIO

    book = "fixture." + "a" * 64
    path = f"/zim/content/{book}/"
    calls = []

    class Response(BytesIO):
        status = 200

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 8081
            assert 2 < timeout <= search.KIWIX_SEARCH_SECONDS

        def request(self, method, url, headers):
            params = parse_qs(urlsplit(url).query)
            assert method == "GET" and urlsplit(url).path == "/zim/search"
            assert params["books.name"] == [book]
            assert params["pattern"] == [query.strip()]
            assert params["format"] == ["xml"]
            start = int(params.get("start", [base])[0])
            offset = start - base
            limit = int(params["pageLength"][0])
            calls.append((start, limit))
            items = "".join(
                f"<item><title>Hit {n}</title><link>{path}{n}</link></item>"
                for n in range(offset, min(offset + limit, 22))
            )
            self.body = Response(
                f'<rss xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><channel>'
                f'<o:startIndex>{start}</o:startIndex>{items}</channel></rss>'.encode()
            )

        def getresponse(self):
            return self.body

        def close(self):
            pass

    monkeypatch.setattr(search.http.client, "HTTPConnection", Connection)
    route = dict(name="Reference", path=path, book_id=book, port=8081)
    targets = {"reference": (route, search.KIWIX_ADAPTER)}
    first, = search.search(query, dict, targets=targets, allowed=lambda *_: True)
    second, = search.search(query, dict, targets=targets, allowed=lambda *_: True,
                            page=2, start_base=first["start_base"])
    assert calls == [(base, 21), (20 + base, 21)]
    assert first["has_more"] and first["page"] == 1
    assert [hit.title for hit in first["results"]] == [f"Hit {n}" for n in range(20)]
    assert not second["has_more"] and second["page"] == 2
    assert [hit.title for hit in second["results"]] == ["Hit 20", "Hit 21"]


@pytest.mark.parametrize("kind", ["pdf", "epub"])
def test_document_search_pages_only_matches(kind, monkeypatch):
    from theaxiom import document_search

    sections = [(n, "cambridge" if n % 2 else "unrelated") for n in range(1, 44)]
    monkeypatch.setattr(document_search, "_load",
                        lambda artifact: dict(sections=sections, partial=True))
    route = dict(kind=kind, name="Document", artifact={}, path="/document")
    first = document_search.search_document(route, "cambridge")
    second = document_search.search_document(dict(route, offset=20), "cambridge")
    assert len(first.results) == 20 and first.has_more and first.partial
    assert len(second.results) == 2 and not second.has_more and second.partial
    suffix = "#page=41" if kind == "pdf" else "/chapter/40"
    assert second.results[0].href == "/document" + suffix


def test_timeout_does_not_hide_other_sources():
    def timed_out(route, query):
        raise TimeoutError

    def successful(route, query):
        return [search.Result("Cambridge", "/fixture/article")]

    targets = {
        "slow": (ROUTE, search.Adapter("Kiwix", timed_out)),
        "fast": (ROUTE, search.Adapter("Kiwix", successful)),
    }
    slow, fast = search.search("cambridge", dict, targets=targets, allowed=lambda *_: True)
    assert slow["status"] == "timeout" and not slow["results"]
    assert fast["status"] == "ok" and len(fast["results"]) == 1


def test_slow_kiwix_book_leaves_time_for_other_books_and_documents(monkeypatch):
    now = 100
    calls = []
    monkeypatch.setattr(search.time, "monotonic", lambda: now)

    def kiwix(route, query):
        nonlocal now
        calls.append(route["name"])
        if route["name"] == "Slow":
            now = route["deadline"]
            raise TimeoutError
        assert route["deadline"] > now
        return [search.Result("Knots", "/fixture/article")]

    def document(route, query):
        calls.append(route["name"])
        return [search.Result("Knots", "/fixture/page")]

    kiwix_adapter = search.Adapter("Kiwix", kiwix)
    monkeypatch.setattr(search, "KIWIX_ADAPTER", kiwix_adapter)
    targets = {
        "slow": (dict(ROUTE, name="Slow"), kiwix_adapter),
        "fast": (dict(ROUTE, name="Fast"), kiwix_adapter),
        "document": (dict(ROUTE, name="Document"), search.Adapter("Document", document)),
    }

    results = search.search("knots", dict, targets=targets, allowed=lambda *_: True)

    assert calls == ["Document", "Slow", "Fast"]
    assert [(group["source_id"], group["status"]) for group in results] == [
        ("slow", "timeout"), ("fast", "ok"), ("document", "ok"),
    ]
