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
        body = b"archive reached"
        self.send_response(200)
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
            "listen 192.168.0.4:80;", f"listen 127.0.0.1:{front_port};")
        # Keep real static icon routing, using checkout content rather than
        # any deployed host path. This runner must never contact Vancouver.
        site = site.replace("/opt/theaxiom/app/deploy/www",
                            str(SITE.parent.parent / "www"))
        site = site.replace("include /etc/nginx/snippets/theaxiom-backend.conf;", "return 404;")
        site = site.replace("proxy_pass http://127.0.0.1:8080/internal/publication/zim;",
                            f"proxy_pass http://127.0.0.1:{gate_port}/internal/publication/zim;")
        site = site.replace("proxy_pass http://127.0.0.1:8082;",
                            f"proxy_pass http://127.0.0.1:{content_port};")
        with tempfile.TemporaryDirectory(prefix="axiom-nginx-auth-") as tmp:
            base = Path(tmp)
            (base / "logs").mkdir()
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
                assert request(front_port, "/zim/viewer") == (200, b"archive reached")
                print("PASS real Nginx icons, publication gate, unpublished denial and reader")
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
