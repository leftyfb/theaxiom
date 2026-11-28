"""Offline fixtures for native disk acquisition, including exercises."""
from contextlib import contextmanager
import hashlib
import io
import sqlite3

import pytest

from theaxiom import kolibri_native as native, kolibri_scope as scope

CID = "c" * 32
ROOT = "a" * 32
TOPIC = "b" * 32
VIDEO = "d" * 32
EXERCISE = "e" * 32
OTHER = "f" * 32


@pytest.fixture
def source(tmp_path, monkeypatch):
    db = tmp_path / "publisher.sqlite3"
    payloads = {"mp4": b"test-video", "vtt": b"WEBVTT\n", "perseus": b"native exercise", "png": b"not selected"}
    files = [(hashlib.md5(data, usedforsecurity=False).hexdigest(), ext, len(data))
             for ext, data in payloads.items()]
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE content_channelmetadata (id, name, version, root_id);
            CREATE TABLE content_contentnode
                (id, channel_id, parent_id, title, kind, tree_id, lft, rght, license_name);
            CREATE TABLE content_file (contentnode_id, local_file_id);
            CREATE TABLE content_localfile (id, extension, file_size_bigint);
        """)
        conn.execute("INSERT INTO content_channelmetadata VALUES (?,?,?,?)", (CID,"Education",5,ROOT))
        conn.executemany("INSERT INTO content_contentnode VALUES (?,?,?,?,?,?,?,?,?)", [
            (ROOT,CID,None,"Root","topic",1,1,10,None),
            (TOPIC,CID,ROOT,"Math","topic",1,2,7,None),
            (VIDEO,CID,TOPIC,"Video","video",1,3,4,"CC BY"),
            (EXERCISE,CID,TOPIC,"Practice","exercise",1,5,6,"Special Permissions"),
            (OTHER,CID,ROOT,"Not selected","video",1,8,9,None),
        ])
        conn.executemany("INSERT INTO content_localfile VALUES (?,?,?)", files)
        conn.executemany("INSERT INTO content_file VALUES (?,?)",
            [(node, f[0]) for node,f in zip([VIDEO,VIDEO,EXERCISE,OTHER],files)])
    metadata = db.read_bytes()
    urls = {native.metadata_url(CID,5):metadata}
    for checksum,ext,_ in files:
        urls[native.STUDIO+"storage/"+scope.ResourceFile(checksum,ext,len(payloads[ext])).storage_path] = payloads[ext]
    requested = []
    @contextmanager
    def transport(url, **kwargs):
        requested.append(url)
        yield io.BytesIO(urls[url])
    monkeypatch.setattr(native.pdf_transport,"open_pdf",transport)
    selected = native.Selection(CID,(TOPIC,),5,hashlib.sha256(metadata).hexdigest(),
                                sum(len(payloads[x]) for x in ("mp4","vtt","perseus")))
    return selected, urls, requested


def test_native_package_includes_exercises_and_no_unselected_files(source,tmp_path):
    selection,urls,requested=source
    root=tmp_path/"package"
    progress=[]
    digest=native.acquire(root,selection,progress=lambda n,t:progress.append((n,t)))
    plan=native.verify(root,selection,digest)
    assert plan.resource_count==2
    assert {item.extension for item in plan.files}=={"mp4","vtt","perseus"}
    assert len(requested)==4
    assert progress[-1]==(selection.media_bytes,selection.media_bytes)
    commands=native.import_commands("/opt/kolibri/bin/kolibri",root,selection)
    assert commands[0][-3:]==["disk",CID,str(root)]
    assert commands[1][3:]==["--node_ids",TOPIC,"--fail-on-error","disk",CID,str(root)]


def test_metadata_pin_and_media_integrity(source,tmp_path):
    selection,urls,requested=source
    urls[native.metadata_url(CID,5)]+=b"changed"
    with pytest.raises(scope.ScopeError,match="metadata changed"):
        native.acquire(tmp_path/"bad",selection)
    assert not (tmp_path/"bad"/native.MANIFEST).exists()


def test_corrupt_media_rejected(source,tmp_path):
    selection,urls,requested=source
    url=next(u for u in urls if u.endswith(".perseus"))
    urls[url]=b"x"*len(urls[url])
    with pytest.raises(scope.ScopeError,match="checksum mismatch"):
        native.acquire(tmp_path/"bad",selection)
    assert not (tmp_path/"bad"/native.MANIFEST).exists()


@pytest.mark.parametrize("mode",["tamper","extra","link","manifest"])
def test_verified_package_fails_closed(source,tmp_path,mode):
    selection,_,_=source
    root=tmp_path/"package"
    digest=native.acquire(root,selection)
    file=next((root/"content"/"storage").rglob("*.perseus"))
    if mode=="tamper":
        file.write_bytes(b"x"*file.stat().st_size)
    elif mode=="extra":
        (file.parent/"extra").write_bytes(b"extra")
    elif mode=="link":
        target=tmp_path/"outside"
        target.write_bytes(file.read_bytes())
        file.unlink()
        file.symlink_to(target)
    else:
        (root/native.MANIFEST).write_text("{}")
    with pytest.raises(scope.ScopeError):
        native.verify(root,selection,digest)


def test_no_unreviewed_full_or_invalid_selection(source,tmp_path):
    selection,_,_=source
    for nodes in [(),("../etc",)]:
        with pytest.raises(scope.ScopeError):
            native.Selection(CID,nodes,5,selection.metadata_sha256,selection.media_bytes)
    with pytest.raises(scope.ScopeError,match="Full-channel"):
        native.acquire(tmp_path/"full",native.Selection(CID,(ROOT,),5,
            selection.metadata_sha256,selection.media_bytes))


def test_resume_reuses_verified_resources_and_replaces_corruption(source, tmp_path):
    selected, urls, requested = source
    root = tmp_path / "package"
    digest = native.acquire(root, selected)
    (root / native.MANIFEST).unlink()
    corrupt = next(root.rglob("*.perseus"))
    corrupt.write_bytes(b"?" * corrupt.stat().st_size)
    requested.clear()
    progress = []
    recovered = native.acquire(root, selected, resume=True,
                               progress=lambda n, t: progress.append(n))
    assert recovered == digest
    assert requested == [next(u for u in urls if u.endswith(".perseus"))]
    assert 0 < progress[0] < selected.media_bytes
    assert progress == sorted(progress)
    native.verify(root, selected, recovered)


def test_retry_preserves_other_resources_and_progress(source, tmp_path, monkeypatch):
    selected, urls, requested = source
    broken = next(u for u in urls if u.endswith(".mp4"))
    attempts = []
    original = native.pdf_transport.open_pdf

    @contextmanager
    def flaky(url, **kwargs):
        if url == broken:
            attempts.append(url)
            if len(attempts) == 1:
                class Interrupted(io.BytesIO):
                    def read(self, size):
                        if self.tell():
                            raise native.pdf_transport.RetryableDownloadError("temporary")
                        return super().read(2)
                yield Interrupted(urls[url])
                return
        with original(url, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(native.pdf_transport, "open_pdf", flaky)
    monkeypatch.setattr(native.pdf_transport, "RETRY_DELAYS", (0,))
    progress = []
    root = tmp_path / "package"
    digest = native.acquire(root, selected, progress=lambda n,t: progress.append(n))
    assert len(attempts) == 2
    assert progress == sorted(progress)
    assert progress[-1] == selected.media_bytes
    assert len(requested) == 4
    native.verify(root, selected, digest)
