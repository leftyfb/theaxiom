from unittest.mock import patch, Mock
from theaxiom import publication

ROUTE = {"path": "/zim/", "port": 8081}

def resolve(xml):
    with patch.object(publication.http.client, "HTTPConnection") as factory:
        response = factory.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = xml.encode()
        return publication.reader_location(ROUTE)

def feed(*links):
    return '<feed xmlns="http://www.w3.org/2005/Atom"><entry>' + ''.join(
        '<link href="' + link + '"/>' for link in links) + '</entry></feed>'

def test_direct_book_reader():
    assert resolve(feed("/zim/content/book-name")) == "/zim/viewer#book-name/"

def test_reject_untrusted_and_ambiguous_book_links():
    for href in ("https://example.com/zim/content/book", "//evil/zim/content/book",
                 "/content/book", "/zim/content/../", "/zim/content/book%2fother"):
        assert resolve(feed(href)) is None
    assert resolve(feed("/zim/content/a", "/zim/content/b")) is None
    assert resolve('<feed xmlns="http://www.w3.org/2005/Atom"/>') is None


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
READY = f"fema-ready.{DIGEST_A}"
OTHER = f"other.{DIGEST_B}"


def test_multi_book_reader_matches_exact_book_without_cross_source_fallback():
    route = {"path": "/zim/", "port": 8082}
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom">'
           f'<entry><link href="/zim/content/{READY}"/></entry>'
           f'<entry><link href="/zim/content/{OTHER}"/></entry></feed>')
    with patch.object(publication.http.client, "HTTPConnection") as factory:
        response = factory.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = xml.encode()
        assert publication.reader_location(route, READY) == f"/zim/viewer#{READY}/"
        assert publication.reader_location(route, OTHER) == f"/zim/viewer#{OTHER}/"
        assert publication.reader_location(route, f"not-published.{DIGEST_A}") is None
        for invalid in ("../../admin", "ready", f"ready.{DIGEST_A.upper()}",
                        f"ready.{DIGEST_A[:-1]}", f"ready..{DIGEST_A}",
                        f"-ready.{DIGEST_A}", f"ready.{DIGEST_A}.extra"):
            assert publication.reader_location(route, invalid) is None


def test_multi_book_rejects_ambiguous_or_external_content_entry():
    route = {"path": "/zim/", "port": 8082}
    for links in (
        f'<link href="/zim/content/{READY}"/><link href="/zim/content/{OTHER}"/>',
        f'<link href="https://elsewhere.example/zim/content/{READY}"/>',
        f'<link href="/zim/content/{READY}%2funsafe"/>',
    ):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry>' + links + '</entry></feed>'
        with patch.object(publication.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = xml.encode()
            assert publication.reader_location(route, READY) is None


def test_generic_reader_port_configuration(monkeypatch):
    from theaxiom import zim_catalog
    monkeypatch.setenv("THEAXIOM_KIWIX_PORT", "8083")
    assert zim_catalog.generic_reader() == {"path": "/zim/", "port": 8083}
    for invalid in ("0", "65536", "-1", "80.5", "abc", " 8082"):
        monkeypatch.setenv("THEAXIOM_KIWIX_PORT", invalid)
        try:
            zim_catalog.generic_reader()
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid port: {invalid!r}")


def test_shared_reader_defaults_to_existing_wikipedia_port(monkeypatch):
    from theaxiom import zim_catalog
    monkeypatch.delenv("THEAXIOM_KIWIX_PORT", raising=False)
    assert zim_catalog.generic_reader() == {"path": "/zim/", "port": 8081}
