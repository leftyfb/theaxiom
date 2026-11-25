"""End-to-end Nginx auth_request smoke test on isolated CI loopback ports.

Uses the real deployment Nginx site with only environment-specific paths
and ports replaced. No host Nginx is started, reloaded or modified.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time

SITE = Path(__file__).resolve().parents[1] / "deploy/nginx/theaxiom.conf"
NAME = "ready." + "a" * 64


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Gate(BaseHTTPRequestHandler):
    def do_GET(self):
        uri = self.headers.get("X-Axiom-Original-URI", "")
        permitted = uri.startswith("/zim/content/" + NAME + "/") or uri == "/zim/viewer"
        self.send_response(204 if permitted else 403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


class Archive(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        body = ((SITE.parent.parent.parent / "templates" / self.path.lstrip("/")).read_bytes()
                if self.path in ("/favicon.png","/favicon.ico") else b"archive reached")
        self.send_response(200)
        if self.path == "/zim/viewer":
            body = b"<html><body>archive reached</body></html>"
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def start_server(port, handler):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def main():
    nginx = shutil.which("nginx") or "/usr/sbin/nginx"
    front_port, gate_port, content_port = free_port(), free_port(), free_port()
    gate = start_server(gate_port, Gate)
    archive = start_server(content_port, Archive)
    try:
        site = SITE.read_text().replace(
            "listen 80;", f"listen 127.0.0.1:{front_port};")
        site = site.replace("listen [::]:80;", "")
        # Keep real static icon routing, using checkout content rather than
        # any deployed host path. This runner must never contact Vancouver.
        site = site.replace("/opt/theaxiom/app/deploy/www",
                            str(SITE.parent.parent / "www"))
        site = site.replace("/opt/theaxiom/app/templates/kiwix-navigation.js",
                            str(SITE.parents[2] / "templates/kiwix-navigation.js"))
        site = site.replace("include /opt/theaxiom/app/templates/theaxiom-backend.conf;", f"proxy_pass http://127.0.0.1:{content_port};")
        site = site.replace("proxy_pass http://127.0.0.1:8080/internal/publication/zim;",
                            f"proxy_pass http://127.0.0.1:{gate_port}/internal/publication/zim;")
        site = site.replace("proxy_pass http://127.0.0.1:8081;",
                            f"proxy_pass http://127.0.0.1:{content_port};")
        with tempfile.TemporaryDirectory(prefix="axiom-nginx-auth-") as tmp:
            base = Path(tmp)
            (base / "logs").mkdir()
            site = site.replace("/var/log/nginx/theaxiom-sources-timing.log",
                                str(base / "logs" / "theaxiom-sources-timing.log"))
            conf = base / "nginx.conf"
            conf.write_text(f"pid {base / 'nginx.pid'};\nevents {{ worker_connections 64; }}\n"
                            f"http {{ access_log off; error_log {base / 'logs' / 'error.log'} warn;\n"
                            + site + "\n}\n")
            process = subprocess.Popen(
                [nginx, "-p", str(base) + "/", "-c", str(conf), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("isolated nginx exited: " + process.stderr.read().decode())
                    try:
                        request(front_port, "/health")
                        break
                    except (ConnectionRefusedError, OSError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("isolated nginx failed to listen")
                for path in ("/api/sources", "/api/sources/"):
                    assert request(front_port, path) == (200, b"archive reached")
                for icon, magic in [
                    ("favicon.svg", b"<svg"),
                    ("favicon.png", bytes.fromhex("89504e470d0a1a0a")),
                    ("favicon.ico", bytes.fromhex("00000100")),
                ]:
                    status, body = request(front_port, "/" + icon)
                    assert status == 200 and body.startswith(magic), (
                        f"unauthenticated Nginx icon {icon} failed: {status}")
                assert request(front_port, "/no-such-icon.ico")[0] == 404
                assert request(front_port, "/zim/content/" + NAME + "/index.html") == (200, b"archive reached")
                calls = len(Archive.requests)
                denied = request(front_port, "/zim/content/other." + "b" * 64 + "/index.html")
                assert denied[0] == 403, denied
                assert len(Archive.requests) == calls, "unpublished archive reached upstream"
                status, viewer = request(front_port, "/zim/viewer")
                assert status == 200 and b'<script src="/zim/axiom-navigation.js"></script>' in viewer
                status, script = request(front_port, "/zim/axiom-navigation.js")
                assert status == 200 and b"followArticle" in script
                timing = (base / "logs" / "theaxiom-sources-timing.log").read_text().splitlines()
                assert len(timing) == 2, timing
                assert all("GET 200 request=" in line and "upstream_header=" in line
                           and "upstream=" in line for line in timing), timing
                print("PASS real Nginx icons, publication gate, reader and source timings")
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
    finally:
        gate.shutdown()
        archive.shutdown()


if __name__ == "__main__":
    main()
