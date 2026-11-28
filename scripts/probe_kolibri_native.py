"""Isolated native Kolibri proof: small official import, HTTPS proxy, offline browser.
Never uses the operator's Kolibri home or Axiom production storage.
"""
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import urlopen
import ssl

from playwright.sync_api import sync_playwright
from theaxiom import kolibri_native as native, kolibri_scope as scope

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
        port, zip_port, https_port, zip_https = free_ports(4)
        env = dict(os.environ, KOLIBRI_HOME=str(home),
                   DJANGO_SETTINGS_MODULE="axiom_kolibri_settings",
                   PYTHONPATH=str(CONFIG))
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
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-keyout",str(root/"key.pem"),
                        "-out",str(root/"cert.pem")],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        main_proxy = (CONFIG / "main-proxy.conf.example").read_text().replace("127.0.0.1:8082",f"127.0.0.1:{port}").replace("/opt/theaxiom/app/deploy/kolibri/navigation.css",str(CONFIG/"navigation.css"))
        zip_proxy = (CONFIG / "zip-proxy.conf.example").read_text().replace("127.0.0.1:8083",f"127.0.0.1:{zip_port}")
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
 access_log off;
 client_body_temp_path {root}/body;
 proxy_temp_path {root}/proxy;
 fastcgi_temp_path {root}/fastcgi;
 uwsgi_temp_path {root}/uwsgi;
 scgi_temp_path {root}/scgi;
 server {{
  listen 127.0.0.1:{https_port} ssl;
  {cert}
  location = / {{ return 200 'Axiom library'; }}
  {main_proxy}
 }}
 server {{
  listen 127.0.0.1:{zip_https} ssl;
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
                    node,title,_=rows["exercise"]
                    page.goto(origin+"/kolibri/viewcontent?node_id="+node,wait_until="domcontentloaded")
                    page.get_by_text(title,exact=True).first.wait_for(timeout=45000)
                    page.locator(".perseus-root .perseus").first.wait_for(state="visible",timeout=45000)
                    assert page.locator(".perseus-root .perseus").first.inner_text().strip()
                    print("NATIVE OFFLINE EXERCISE RENDER PASSED",flush=True)
                    print("EXTERNAL REQUEST HOSTS BLOCKED",sorted(blocked),flush=True)
                    browser.close()
            except BaseException:
                print((root/"kolibri.log").read_text(errors="replace")[-12000:],flush=True)
                if (root/"nginx.log").exists():
                    print((root/"nginx.log").read_text(errors="replace")[-4000:],flush=True)
                if "page" in locals():
                    try: print("PAGE",page.url,page.locator("body").inner_text()[:2000],flush=True)
                    except Exception: pass
                raise
            finally:
                for process in (proxy,server):
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=15)
                        except subprocess.TimeoutExpired: process.kill(); process.wait()
if __name__=="__main__":
    main()
