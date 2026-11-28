"""One-time live compatibility verification on tux; never touches production."""
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import socket
from urllib.request import urlopen
from urllib.parse import urlencode

from theaxiom import kolibri_bundle as bundle, kolibri_scope as scope, zim_reader_probe, zim_source
from playwright.sync_api import sync_playwright

CID = "c9d7f950ab6b5a1199e3d6c10d7f0103"
with tempfile.TemporaryDirectory(prefix="axiom-kolibri-live-") as temp:
    root = Path(temp)
    latest, _ = bundle.fetch_metadata(CID, root/"latest.sqlite3")
    channel, digest = bundle.fetch_metadata(CID, root/"version.sqlite3", version=latest.version)
    assert channel == latest
    print("PINNED EDITION", channel.version, digest, flush=True)
    with sqlite3.connect((root/"version.sqlite3").absolute().as_uri()+"?mode=ro&immutable=1", uri=True) as conn:
        candidates = conn.execute("""
            SELECT n.id,n.title,SUM(l.file_size) AS total
            FROM content_contentnode n JOIN content_file f ON f.contentnode_id=n.id
            JOIN content_localfile l ON l.id=f.local_file_id
            WHERE n.kind='video' AND n.license_name='CC BY-NC-SA'
            GROUP BY n.id,n.title
            HAVING total BETWEEN 100000 AND 5000000
                AND SUM(CASE WHEN l.extension='mp4' THEN 1 ELSE 0 END)>0
                AND SUM(CASE WHEN l.extension='vtt' THEN 1 ELSE 0 END)>0
            ORDER BY total LIMIT 30
        """).fetchall()
    assert candidates, "No bounded video with captions available"
    selected = None
    for node,title,_ in candidates:
        try:
            selected = bundle.select(root/"version.sqlite3",CID,[node])
            if selected.total_bytes <= 5_000_000:
                break
        except scope.ScopeError:
            pass
    assert selected and selected.total_bytes <= 5_000_000
    lesson = selected.lessons[0]
    print("SELECTED LESSON", lesson.node_id, lesson.title, selected.total_bytes,
          [(f.extension,f.size) for f in selected.files], flush=True)
    config = bundle.fields(selected,[lesson.node_id],digest,"eng") | {"format":"zim"}
    assert zim_source.candidate(config)[1:] == (None,None)
    pages = root/"pages"; pages.mkdir()
    bundle.acquire(selected,pages)
    archive = root/"education.zim"
    bundle.build_zim(selected,pages,archive)
    with archive.open("rb") as stream:
        zim_source.check_header(stream.read(80),archive.stat().st_size)
    print("GENERATED ZIM",archive.stat().st_size,flush=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
    process = subprocess.Popen(["kiwix-serve","--address=127.0.0.1",f"--port={port}",
                                "--urlRootLocation=/zim/",str(archive)],
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                names=zim_reader_probe.listed_book_ids(port=port)
                if names: break
            except (OSError,ValueError): pass
            time.sleep(.25)
        else: raise RuntimeError("Isolated Kiwix failed to start")
        name=next(iter(names)); origin=f"http://127.0.0.1:{port}"
        url=origin+"/zim/content/"+name+"/"+lesson.node_id+".html"
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            context=browser.new_context(viewport={"width":390,"height":844})
            context.route("**/*",lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
            page=context.new_page(); page.goto(url,wait_until="domcontentloaded")
            assert lesson.title in page.locator("h1").inner_text()
            assert page.locator("video").count()>0
            page.locator("video").first.evaluate("(video) => { video.muted=true; return video.play(); }")
            page.wait_for_function("document.querySelector('video').currentTime > 0",timeout=15000)
            assert page.locator("track").count()>0
            assert "Transcript" in page.locator("body").inner_text()
            print("OFFLINE MOBILE PLAYBACK AND CAPTIONS PASSED",flush=True)
            browser.close()
        pattern=next(word for word in lesson.title.split() if len(word)>3)
        with urlopen(origin+"/zim/search?"+urlencode({"books.name":name,"pattern":pattern}),timeout=15) as response:
            data=response.read(1024*1024)
            assert response.status==200 and lesson.node_id.encode() in data
        print("KIWIX INDEXED LESSON SEARCH PASSED",flush=True)
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
