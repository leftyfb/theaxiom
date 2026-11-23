from unittest.mock import Mock

from theaxiom import legacy_alias


LEGACY_BOOK = "wikipedia_en_all_maxi_2026-08"
MANAGED_BOOK = "wikipedia-en.34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6"
ROUTE = {"book_id": LEGACY_BOOK}


def resolve(path, query="", *, managed=False, published=True, book=MANAGED_BOOK):
    catalog = Mock()
    catalog.source_content_name.return_value = book
    return legacy_alias.resolve_wikipedia(
        path, query, records={"source_id": "wikipedia-en"}, route=ROUTE,
        published=published, managed=managed, zim_catalog=catalog,
    )


def test_unadopted_aliases_use_pinned_legacy_book_and_preserve_query():
    # Fragments never reach the HTTP server; a redirect Location without a
    # fragment lets the browser retain the original fragment automatically.
    assert resolve("/wikipedia/content/Main_Page", "x=1&x=2") == (
        "/zim/content/" + LEGACY_BOOK + "/Main_Page?x=1&x=2"
    )
    assert resolve("/wikipedia/search", "q=climate&books.order=title") == (
        "/zim/search?books.name=" + LEGACY_BOOK + "&q=climate&books.order=title"
    )


def test_adopted_aliases_resolve_against_installed_managed_artifact():
    assert resolve("/wikipedia/content/Main_Page", managed=True) == (
        "/zim/content/" + MANAGED_BOOK + "/Main_Page"
    )
    assert resolve("/wikipedia/viewer", "foo=bar", managed=True) == "/zim/viewer?foo=bar"


def test_viewer_fragment_translates_legacy_book_to_managed_book():
    assert legacy_alias.translate_viewer_fragment(
        "#" + LEGACY_BOOK + "/Main_Page", LEGACY_BOOK, MANAGED_BOOK
    ) == "#" + MANAGED_BOOK + "/Main_Page"
    assert legacy_alias.translate_viewer_fragment(
        "#other-book/Main_Page", LEGACY_BOOK, MANAGED_BOOK
    ) == "#other-book/Main_Page"


def test_viewer_redirect_document_rewrites_hash_before_managed_viewer():
    body = legacy_alias.viewer_redirect_document(LEGACY_BOOK, MANAGED_BOOK, nonce="test-nonce")
    assert '<script nonce="test-nonce">' in body
    assert "location.hash" in body
    assert "location.replace('/zim/viewer'+location.search+hash)" in body
    assert LEGACY_BOOK in body and MANAGED_BOOK in body


def test_book_qualified_content_links_drop_stale_book_component():
    assert resolve("/wikipedia/content/" + LEGACY_BOOK + "/Main_Page", managed=True) == (
        "/zim/content/" + MANAGED_BOOK + "/Main_Page"
    )


def test_search_has_one_canonical_native_book_selector():
    assert resolve("/wikipedia/search", "books.name=stale&q=climate&books.name=other", managed=True) == (
        "/zim/search?books.name=" + MANAGED_BOOK + "&q=climate"
    )


def test_unpublished_alias_is_denied_before_or_after_adoption():
    assert resolve("/wikipedia/viewer", published=False) is None
    assert resolve("/wikipedia/content/Main_Page", managed=True, published=False) is None


def test_unknown_routes_are_not_redirected():
    assert resolve("/wikipedia/not-a-reader-route") is None
    assert resolve("/zim/content/Main_Page") is None
