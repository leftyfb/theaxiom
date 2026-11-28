import sqlite3

import pytest

from theaxiom.kolibri_publication import Registry

CID = "a"*32
DIGEST = "b"*64
CHECKSUM = "c"*32
SOURCE = {"school": {"channel_id": CID, "version": 5, "manifest_sha256": DIGEST}}


def fixture(tmp_path):
    db = tmp_path / "db.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE content_channelmetadata (id, version);
        CREATE TABLE content_contentnode (id, available);
        CREATE TABLE content_file (local_file_id, contentnode_id);
        CREATE TABLE content_localfile (id, extension, available);
        """)
        conn.execute("INSERT INTO content_channelmetadata VALUES (?,5)", (CID,))
        conn.execute("INSERT INTO content_contentnode VALUES ('node',1)")
        conn.execute("INSERT INTO content_file VALUES (?,'node')", (CHECKSUM,))
        conn.execute("INSERT INTO content_localfile VALUES (?,?,1)", (CHECKSUM,"mp4"))
    return Registry(tmp_path/"private", db)


def test_both_origins_and_transactions(tmp_path):
    registry = fixture(tmp_path)
    assert not registry.authorize("/kolibri/en/learn/")
    with registry.transaction() as acknowledge:
        assert not registry.authorize("/kolibri/en/learn/")
        acknowledge(SOURCE)
        assert not registry.authorize("/content/static/sandbox/test.html", alternate=True)
    assert registry.acknowledged() == SOURCE
    assert registry.authorize("/kolibri/en/learn/")
    assert registry.authorize("/content/static/sandbox/test.html", alternate=True)
    assert registry.authorize(f"/kolibri/content/storage/c/c/{CHECKSUM}.mp4")
    assert not registry.authorize(f"/kolibri/content/storage/c/c/{'d'*32}.mp4")
    assert not registry.authorize("/kolibri/content/databases/anything.sqlite3")
    assert not registry.authorize("/kolibri/api/content/contentnode/?no_available_filtering=true")
    assert not registry.authorize("/kolibri/api/content/contentnode/?no_available_%66iltering=true")
    with sqlite3.connect(registry.database) as conn:
        conn.execute("INSERT INTO content_channelmetadata VALUES ('unacknowledged',5)")
    assert not registry.authorize("/kolibri/en/learn/")


def test_failed_transaction_stays_closed(tmp_path):
    registry = fixture(tmp_path)
    with pytest.raises(ValueError):
        with registry.transaction() as acknowledge:
            acknowledge({})
    assert registry.pending.exists()
    assert not registry.authorize("/kolibri/en/learn/")
    with pytest.raises(ValueError):
        with registry.transaction():
            pass
