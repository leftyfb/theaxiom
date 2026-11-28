"""Isolated native Kolibri proof: small official import, HTTPS proxy, offline browser.
Never uses the operator's Kolibri home or Axiom production storage.
"""
import os
import json
import re
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from playwright.sync_api import sync_playwright
from theaxiom import kolibri_native as native, kolibri_scope as scope
from theaxiom.kolibri_publication import Registry, verify_live_scope

CID = "c9d7f950ab6b5a1199e3d6c10d7f0103"
CONFIG = Path(__file__).resolve().parents[1] / "deploy" / "kolibri"

def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()

def main():
    with tempfile.TemporaryDirectory(prefix="axiom-kolibri-native-") as temp:
        root = Path(temp)
        home = root / "kolibri"
        home.mkdir()
        port, zip_port, https_port, zip_https, http_port, zip_http = free_ports(6)
        env = dict(os.environ, KOLIBRI_HOME=str(home),
                   DJANGO_SETTINGS_MODULE="axiom_kolibri_settings",
                   PYTHONPATH=str(CONFIG), AXIOM_KOLIBRI_HTTP_ZIP_PORT=str(zip_http))
        options = (CONFIG / "options.ini.example").read_text()
        options = options.replace("HTTP_PORT = 8082", f"HTTP_PORT = {port}")
        options = options.replace("ZIP_CONTENT_PORT = 8083", f"ZIP_CONTENT_PORT = {zip_port}")
        options = options.replace("ZIP_CONTENT_ORIGIN = 8443", f"ZIP_CONTENT_ORIGIN = {zip_https}")
        (home / "options.ini").write_text(options)
        kolibri = shutil.which("kolibri")
        assert kolibri, "Kolibri executable missing"
        nginx = shutil.which("nginx")
        assert nginx, "Nginx executable missing"
        def manage(*args):
            subprocess.run([kolibri, "manage", *args], env=env, check=True, timeout=240)
        manage("provisiondevice", "--noinput", "--facility", "Axiom native probe",
               "--preset", "informal", "--language_id", "en", "--superusername", "probe",
               "--superuserpassword", secrets.token_urlsafe(24))
        db = root / "preview.sqlite3"
        channel, metadata_digest, metadata_size = native.fetch_metadata(CID, db)
        print("PINNED CHANNEL",channel.version,metadata_digest,metadata_size,flush=True)
        with sqlite3.connect(db.absolute().as_uri()+"?mode=ro", uri=True) as conn:
            rows = {}
            for kind, required in (("video", "mp4"), ("exercise", "perseus")):
                extra = "AND SUM(CASE WHEN l.extension='vtt' THEN 1 ELSE 0 END)>0" if kind=="video" else ""
                row = conn.execute("""
                    SELECT n.id,n.title,SUM(l.file_size) AS total
                    FROM content_contentnode n JOIN content_file f ON f.contentnode_id=n.id
                    JOIN content_localfile l ON l.id=f.local_file_id
                    WHERE n.kind=? AND n.license_name IS NOT NULL AND n.license_name != ''
                    GROUP BY n.id,n.title
                    HAVING total BETWEEN 100 AND 5000000
                    AND SUM(CASE WHEN l.extension=? THEN 1 ELSE 0 END)>0
                    """ + extra + " ORDER BY total LIMIT 1", (kind, required)).fetchone()
                assert row, f"No bounded {kind} in publisher metadata"
                rows[kind] = row
                print("SELECTED",kind,row,flush=True)
        nodes=tuple(r[0] for r in rows.values())
        plan=scope.plan_scope(db,CID,nodes)
        assert plan.total_bytes < 10_000_000, "Probe media exceeded bounded scope"
        selection=native.Selection(CID,nodes,channel.version,metadata_digest,plan.total_bytes)
        package=root/"verified-package"
        digest=native.acquire(package,selection)
        native.verify(package,selection,digest)
        print("VERIFIED NATIVE DISK PACKAGE",plan.total_bytes,digest,flush=True)
        for command in native.import_commands(kolibri,package,selection):
            subprocess.run(command,env=env,check=True,timeout=240)
        print("NATIVE DISK IMPORT COMPLETE",flush=True)
        registry = Registry(root/"private-publications", home/"db.sqlite3")
        verify_live_scope(registry, db, selection)
        publication_record = {"khan-probe": {"channel_id": CID, "version": channel.version, "manifest_sha256": digest}}
        assert not registry.authorize("/kolibri/en/learn/")
        with registry.transaction() as acknowledge:
            acknowledge(publication_record)
            assert not registry.authorize("/kolibri/en/learn/")
        assert registry.authorize("/kolibri/en/learn/")
        assert registry.authorize("/content/static/sandbox/example.html",alternate=True)
        print("NATIVE PUBLICATION ACKNOWLEDGEMENT PASSED",flush=True)
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-keyout",str(root/"key.pem"),
                        "-out",str(root/"cert.pem")],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Gate(BaseHTTPRequestHandler):
            def do_GET(self):
                allowed = registry.authorize(self.headers.get("X-Axiom-Original-URI", ""),
                    alternate=self.path.endswith("-alternate"))
                self.send_response(204 if allowed else 403)
                self.send_header("Content-Length", "0")
                self.end_headers()
        gate_server = ThreadingHTTPServer(("127.0.0.1", 0), Gate)
        gate_thread = Thread(target=gate_server.serve_forever, daemon=True)
        gate_thread.start()
        gate_port = gate_server.server_address[1]
        main_proxy = (CONFIG / "main-proxy.conf.example").read_text().replace("127.0.0.1:8082",f"127.0.0.1:{port}").replace("/opt/theaxiom/app/deploy/kolibri/navigation.css",str(CONFIG/"navigation.css"))
        zip_proxy = (CONFIG / "zip-proxy.conf.example").read_text().replace("127.0.0.1:8083",f"127.0.0.1:{zip_port}")
        main_proxy = main_proxy.replace('"__zipContentPort":"8443"',f'"__zipContentPort":"{zip_https}"')
        origin_map = (CONFIG/"origin-map.conf.example").read_text().replace("8084",str(zip_http)).replace("8443",str(zip_https))
        main_proxy = main_proxy.replace("127.0.0.1:8080",f"127.0.0.1:{gate_port}")
        zip_proxy = zip_proxy.replace("127.0.0.1:8080",f"127.0.0.1:{gate_port}")
        for name in ("body","proxy","fastcgi","uwsgi","scgi"):
            (root/name).mkdir()
        cert = f"ssl_certificate {root}/cert.pem; ssl_certificate_key {root}/key.pem;"
        conf = root/"nginx.conf"
        conf.write_text(f"""
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx.log info;
events {{ worker_connections 128; }}
http {{
 {origin_map}
 access_log off;
 client_body_temp_path {root}/body;
 proxy_temp_path {root}/proxy;
 fastcgi_temp_path {root}/fastcgi;
 uwsgi_temp_path {root}/uwsgi;
 scgi_temp_path {root}/scgi;
 server {{
  listen 127.0.0.1:{https_port} ssl;
  listen 127.0.0.1:{http_port};
  {cert}
  location = / {{ return 200 'Axiom library'; }}
  {main_proxy}
 }}
 server {{
  listen 127.0.0.1:{zip_https} ssl;
  listen 127.0.0.1:{zip_http};
  {cert}
  {zip_proxy}
 }}
}}
""")
        subprocess.run([nginx,"-t","-p",str(root),"-c",str(conf)],check=True)
        with (root/"kolibri.log").open("w") as log:
            server = subprocess.Popen([kolibri,"start","--foreground"],env=env,stdout=log,stderr=subprocess.STDOUT)
            proxy = subprocess.Popen([nginx,"-p",str(root),"-c",str(conf),"-g","daemon off;"])
            origin=f"https://localhost:{https_port}"
            zip_origin=f"https://localhost:{zip_https}"
            try:
                for _ in range(90):
                    if server.poll() is not None:
                        raise RuntimeError("Kolibri exited before serving")
                    try:
                        with urlopen(origin+"/kolibri/",context=ssl._create_unverified_context(),timeout=2) as response:
                            if response.status==200:
                                break
                    except OSError:
                        time.sleep(1)
                else:
                    raise RuntimeError("Kolibri proxy failed to become ready")
                print("NATIVE HTTPS PROXY READY",flush=True)
                import kolibri as vendor_kolibri
                sandbox = (Path(vendor_kolibri.__file__).parent/"core/content/build/sandbox_filename").read_text().strip()
                sandbox_path = "/content/static/sandbox/" + sandbox
                with urlopen(zip_origin+sandbox_path,context=ssl._create_unverified_context(),timeout=10) as response:
                    assert response.status == 200 and b"<" in response.read(512)
                with urlopen(origin+"/kolibri/en/learn/",context=ssl._create_unverified_context(),timeout=10) as response:
                    assert f"https://*:{zip_https}" in response.headers.get("Content-Security-Policy", "")
                print("SEPARATE HTTPS SANDBOX ENDPOINT PASSED",flush=True)
                with urlopen(f"http://localhost:{http_port}/kolibri/en/learn/",timeout=10) as response:
                    assert f"http://*:{zip_http}" in response.headers.get("Content-Security-Policy", "")
                    assert f'"__zipContentPort":"{zip_http}"' in response.read().decode()
                with urlopen(f"http://localhost:{zip_http}"+sandbox_path,timeout=10) as response:
                    assert response.status == 200
                print("SCHEME-SPECIFIC HTTP SANDBOX ENDPOINT PASSED",flush=True)
                def public_json(path, params=None):
                    url=origin+"/kolibri/api/public/v2/"+path
                    if params:
                        url+="?"+urlencode(params)
                    with urlopen(url,context=ssl._create_unverified_context(),timeout=10) as response:
                        raw=response.read(2*1024*1024+1)
                    assert len(raw)<=2*1024*1024, "Native API response too large"
                    return json.loads(raw)
                video_node=rows["video"][0]
                def search_selected():
                    answer=public_json("contentnode/",{"channels":CID,"keywords":"Evaluating functions",
                        "kind":"content","max_results":20})
                    hits=answer["results"] if isinstance(answer,dict) else answer
                    assert any(hit.get("id")==video_node for hit in hits),hits[:2]
                search_selected()
                video_info=public_json("contentnode/"+video_node+"/")
                caption=next(f for f in video_info["files"] if f["extension"]=="vtt" and f["available"])
                caption_url=urljoin(origin+"/kolibri/",caption["storage_url"])
                assert caption_url.startswith(origin+"/kolibri/")
                with urlopen(caption_url,context=ssl._create_unverified_context(),timeout=10) as response:
                    assert response.read(64).lstrip(b"\\xef\\xbb\\xbf").startswith(b"WEBVTT")
                print("LOCAL PUBLIC API SEARCH AND CAPTION FILE PASSED",flush=True)
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    ctx=browser.new_context(ignore_https_errors=True,viewport={"width":390,"height":844},service_workers="block")
                    blocked=set()
                    def local_only(route):
                        if route.request.url.startswith((origin+"/",zip_origin+"/")):
                            route.continue_()
                        else:
                            blocked.add(route.request.url.split("/")[2] if "://" in route.request.url else route.request.url)
                            route.abort()
                    ctx.route("**/*",local_only)
                    page=ctx.new_page()
                    page.on("pageerror",lambda e: print("BROWSER ERROR",str(e)[:500],flush=True))
                    page.goto(origin+"/kolibri/en/learn/",wait_until="domcontentloaded")
                    back=page.get_by_role("link",name="Back to library")
                    back.wait_for(state="visible",timeout=30000)
                    assert back.get_attribute("href")=="/"
                    back.click()
                    page.wait_for_url(origin+"/")
                    assert "Axiom library" in page.locator("body").inner_text()
                    print("MOBILE BACK TO LIBRARY PASSED",flush=True)
                    node,title,_=rows["video"]
                    page.goto(origin+"/kolibri/viewcontent?node_id="+node,wait_until="domcontentloaded")
                    page.get_by_text(title,exact=True).first.wait_for(timeout=45000)
                    video=page.locator("video").first
                    video.wait_for(state="attached",timeout=30000)
                    video.evaluate("(v) => { v.muted=true; return v.play(); }")
                    page.wait_for_function("document.querySelector('video').currentTime > 0",timeout=15000)
                    print("NATIVE OFFLINE VIDEO PLAYBACK PASSED",flush=True)
                    page.set_viewport_size({"width":1280,"height":900})
                    video.hover()
                    captions=page.locator(".vjs-captions-button button, button.vjs-captions-button").first
                    captions.wait_for(state="visible",timeout=15000)
                    captions.click()
                    transcript=page.get_by_text("Transcript",exact=True).first
                    transcript.wait_for(state="visible",timeout=10000)
                    transcript.click()
                    page.locator(".media-player-transcript.showing").wait_for(state="visible",timeout=15000)
                    print("NATIVE OFFLINE CAPTION TRANSCRIPT UI PASSED",flush=True)
                    page.set_viewport_size({"width": 1280, "height": 900})
                    node,title,_=rows["exercise"]
                    page.goto(origin+"/kolibri/viewcontent?node_id="+node,wait_until="domcontentloaded")
                    page.get_by_text(title,exact=True).first.wait_for(timeout=45000)
                    # The Perseus container becomes visible before its asynchronous
                    # ZIP/question rendering completes. Wait for actual question UI.
                    page.locator(".perseus-root .perseus").first.wait_for(state="visible",timeout=45000)
                    page.wait_for_function("""() => {
                      const el = document.querySelector('.perseus-root .perseus');
                      return el && el.innerText.trim().length > 0 &&
                        el.querySelector('input,textarea,[contenteditable=true],button,[role=radio],.mq-editable-field');
                    }""",timeout=45000)
                    print("EXERCISE QUESTION",page.locator(".perseus-root .perseus").first.inner_text()[:300],flush=True)
                    print("NATIVE OFFLINE EXERCISE RENDER PASSED",flush=True)
                    field=page.locator(".perseus-root .perseus .mq-editable-field:visible")
                    if field.count():
                        field.first.click()
                        page.keyboard.type("1")
                    else:
                        answer=page.locator(".perseus-root .perseus input:not([type=radio]):visible, .perseus-root .perseus textarea:visible, .perseus-root .perseus [contenteditable=true]:visible")
                        if answer.count():
                            answer.first.click()
                            page.keyboard.type("1")
                        else:
                            choice=page.locator(".perseus-root .perseus [role=radio]:visible, .perseus-root .perseus input[type=radio]:visible")
                            assert choice.count(), "No accessible answer widget"
                            choice.first.click()
                    check=page.get_by_role("button",name="Check",exact=True)
                    check.focus()
                    check.press("Space")
                    try:
                        page.locator(".current-status").filter(has_text=re.compile(r"(Correct!|Try again|Great! Keep going|Try a different question|Try next question)")).first.wait_for(state="visible",timeout=30000)
                    except BaseException:
                        print("EXERCISE BODY",page.locator("body").inner_text()[:3000],flush=True)
                        print("EXERCISE INPUTS",page.locator(".perseus-root input, .perseus-root textarea").evaluate_all("(els) => els.map(e => ({type:e.type,value:e.value,outer:e.outerHTML.slice(0,500)}))"),flush=True)
                        raise
                    print("OFFLINE EXERCISE ANSWER CHECK PASSED",flush=True)
                    print("EXTERNAL REQUEST HOSTS BLOCKED",sorted(blocked),flush=True)
                    browser.close()
                manage("shell","-c",f"""
from kolibri.core.auth.models import FacilityUser
from kolibri.core.content.models import ContentNode
from kolibri.core.logger.models import ContentSummaryLog
from kolibri.utils.time_utils import local_now
learner=FacilityUser.objects.create_user("probe_learner", password={secrets.token_urlsafe(24)!r}, full_name="Probe Learner")
node=ContentNode.objects.get(pk={video_node!r})
ContentSummaryLog.objects.create(user=learner, content_id=node.content_id, channel_id={CID!r},
    start_timestamp=local_now(), end_timestamp=local_now(), kind="video", progress=.5, time_spent=30)
""")
                # Native removal must not expose stale published content or
                # destroy the independent verified package or learner accounts.
                with registry.transaction() as acknowledge:
                    assert not registry.authorize("/kolibri/en/learn/")
                    manage("deletechannel",CID)
                    acknowledge({})
                assert not registry.authorize("/kolibri/en/learn/")
                assert not registry.authorize("/content/static/sandbox/example.html",alternate=True)
                for _ in range(20):
                    try:
                        public_json("contentnode/"+video_node+"/",{"axiom_probe":secrets.token_hex(4)})
                    except HTTPError as exc:
                        if exc.code==403:
                            break
                        raise
                    time.sleep(.5)
                else:
                    raise AssertionError("Removed channel remains visible in native API")
                try:
                    with urlopen(caption_url,context=ssl._create_unverified_context(),timeout=10) as response:
                        raise AssertionError("Removed channel still serves its unique caption")
                except HTTPError as exc:
                    assert exc.code==403,exc.code
                # Both public origins must reject requests after unpublication.
                for url in (origin+"/kolibri/en/learn/", zip_origin+"/content/static/sandbox/example.html"):
                    try:
                        with urlopen(url,context=ssl._create_unverified_context(),timeout=10):
                            raise AssertionError("Unpublished reader route accessible")
                    except HTTPError as exc:
                        assert exc.code==403
                native.verify(package,selection,digest)
                print("NATIVE CHANNEL REMOVAL AND PRIVATE PACKAGE PRESERVATION PASSED",flush=True)
                with registry.transaction() as acknowledge:
                    for command in native.import_commands(kolibri,package,selection):
                        subprocess.run(command,env=env,check=True,timeout=240)
                    verify_live_scope(registry, db, selection)
                    acknowledge(publication_record)
                assert registry.authorize("/kolibri/en/learn/")
                search_selected()
                manage("shell","-c",'from kolibri.core.auth.models import FacilityUser; assert FacilityUser.objects.filter(username="probe").exists()')
                manage("shell","-c",f"""
from kolibri.core.auth.models import FacilityUser
from kolibri.core.logger.models import ContentSummaryLog
learner=FacilityUser.objects.get(username="probe_learner")
assert ContentSummaryLog.objects.filter(user=learner, channel_id={CID!r}, progress=.5,time_spent=30).exists()
""")
                print("NATIVE CHANNEL RESTORE AND LEARNER ACCOUNT/PROGRESS PRESERVATION PASSED",flush=True)
            except BaseException:
                print((root/"kolibri.log").read_text(errors="replace")[-12000:],flush=True)
                if (root/"nginx.log").exists():
                    print((root/"nginx.log").read_text(errors="replace")[-4000:],flush=True)
                if "page" in locals():
                    try: print("PAGE",page.url,page.locator("body").inner_text()[:2000],flush=True)
                    except Exception: pass
                raise
            finally:
                gate_server.shutdown()
                gate_server.server_close()
                for process in (proxy,server):
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=15)
                        except subprocess.TimeoutExpired: process.kill(); process.wait()
if __name__=="__main__":
    main()
