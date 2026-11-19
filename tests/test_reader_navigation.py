from unittest.mock import patch, Mock
from theaxiom import publication

ROUTE = publication.ROUTES["wikipedia-en"]

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
    assert resolve(feed("/wikipedia/content/book-name")) == "/wikipedia/viewer#book-name/"

def test_reject_untrusted_and_ambiguous_book_links():
    for href in ("https://example.com/wikipedia/content/book", "//evil/wikipedia/content/book",
                 "/content/book", "/wikipedia/content/../", "/wikipedia/content/book%2fother"):
        assert resolve(feed(href)) is None
    assert resolve(feed("/wikipedia/content/a", "/wikipedia/content/b")) is None
    assert resolve('<feed xmlns="http://www.w3.org/2005/Atom"/>') is None
