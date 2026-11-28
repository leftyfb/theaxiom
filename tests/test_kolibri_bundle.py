import hashlib
from pathlib import Path
import sqlite3

import pytest

from theaxiom import kolibri_bundle as bundle

CID = "a" * 32
ROOT = "b" * 32
TOPIC = "c" * 32
LESSON = "d" * 32
OTHER = "e" * 32
PAYLOAD = b"example-video"
MD5 = hashlib.md5(PAYLOAD, usedforsecurity=False).hexdigest()


def fixture(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE content_channelmetadata(id,name,version,root_id);
            CREATE TABLE content_contentnode(id,parent_id,channel_id,title,kind,license_name,tree_id,lft,rght,description,author);
            CREATE TABLE content_file(contentnode_id,local_file_id);
            CREATE TABLE content_localfile(id,extension,file_size);
        """)
        conn.execute("INSERT INTO content_channelmetadata VALUES (?,?,?,?)", (CID,"Education",1,ROOT))
        conn.executemany("INSERT INTO content_contentnode VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (ROOT,None,CID,"Root","topic",None,1,1,8,"",""),
            (TOPIC,ROOT,CID,"Math","topic",None,1,2,5,"",""),
            (LESSON,TOPIC,CID,"Fractions <script>","video","CC BY-NC-SA",1,3,4,"Lesson <b>","Author"),
            (OTHER,ROOT,CID,"Unselected","video","CC BY",1,6,7,"",""),
        ])
        conn.execute("INSERT INTO content_localfile VALUES (?,?,?)", (MD5,"mp4",len(PAYLOAD)))
        conn.executemany("INSERT INTO content_file VALUES (?,?)", [(LESSON,MD5),(OTHER,MD5)])
    return path


def test_scoped_reader(tmp_path):
    selected = bundle.select(fixture(tmp_path/"channel.sqlite3"),CID,[TOPIC])
    assert len(selected.lessons) == 1
    assert selected.total_bytes == len(PAYLOAD)
    assert [t[0] for t in selected.topics] == [ROOT,TOPIC]
    pages = tmp_path/"pages"
    pages.mkdir()
    bundle.render(selected,pages)
    assert not (pages/(OTHER+".html")).exists()
    html = (pages/(LESSON+".html")).read_text()
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<video controls" in html and MD5+".mp4" in html
    assert "CC BY-NC-SA" in html and "Author" in html
    assert LESSON+".html" in (pages/(TOPIC+".html")).read_text()


def test_unknown_license_excluded(tmp_path):
    path = fixture(tmp_path/"channel.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE content_contentnode SET license_name='All Rights Reserved' WHERE id=?", (LESSON,))
    with pytest.raises(bundle.scope.ScopeError, match="No supported"):
        bundle.select(path,CID,[TOPIC])


def test_generated_configuration_requires_scope_and_distinct_identity(tmp_path):
    path = fixture(tmp_path/"channel.sqlite3")
    selected = bundle.select(path, CID, [TOPIC])
    fields = bundle.fields(selected, [TOPIC], "f"*64, "eng")
    config = fields | {"format":"zim"}
    assert bundle.configuration(config) == (CID, [TOPIC], 1, "f"*64, len(PAYLOAD), "eng")
    with pytest.raises(bundle.scope.ScopeError):
        bundle.configuration(config | {"generator_scope":""})
    with pytest.raises(bundle.scope.ScopeError):
        bundle.configuration(config | {"sha256":"f"*64})
    with pytest.raises(bundle.scope.ScopeError):
        bundle.configuration(config | {"source_url":"https://example.org/archive.zim"})


def test_unsupported_interactive_items_not_rendered(tmp_path):
    path = fixture(tmp_path/"channel.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE content_contentnode SET kind='exercise' WHERE id=?", (LESSON,))
    with pytest.raises(bundle.scope.ScopeError, match="No supported"):
        bundle.select(path,CID,[TOPIC])


def test_upstream_md5_mismatch_fails_before_reader(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from io import BytesIO
    selected = bundle.select(fixture(tmp_path/"channel.sqlite3"),CID,[TOPIC])
    @contextmanager
    def fake(*args, **kwargs):
        yield BytesIO(b"x"*len(PAYLOAD))
    monkeypatch.setattr(bundle.pdf_transport, "open_pdf", fake)
    pages = tmp_path/"pages"; pages.mkdir()
    with pytest.raises(bundle.scope.ScopeError, match="checksum"):
        bundle.acquire(selected,pages)
    assert not (pages/"index.html").exists()
