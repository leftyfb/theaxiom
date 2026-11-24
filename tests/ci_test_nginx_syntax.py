"""Parse the actual site file with nginx in an isolated CI sandbox.

Never reload, start, or change the system nginx service. Substitute the
deployment-only include and bind address in-memory to validate syntax without
requiring Vancouver paths or live upstreams.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    nginx = shutil.which("nginx") or "/usr/sbin/nginx"
    site = (ROOT / "deploy/nginx/theaxiom.conf").read_text()
    assert "location ^~ /wikipedia/" not in site
    assert "auth_request /_publication_zim;" in site
    site = site.replace("listen 80;", "listen 127.0.0.1:18080;")
    site = site.replace("listen [::]:80;", "")
    site = site.replace("include /opt/theaxiom/app/templates/theaxiom-backend.conf;",
                        "proxy_pass http://127.0.0.1:8080;")
    with tempfile.TemporaryDirectory(prefix="axiom-nginx-ci-") as temp:
        base = Path(temp)
        (base / "logs").mkdir()
        config = base / "nginx.conf"
        config.write_text("pid " + str(base / "nginx.pid") + ";\n"
                          "events { worker_connections 16; }\n"
                          "http { access_log off; error_log " + str(base / "logs/error.log") + " warn;\n"
                          + site + "\n}\n")
        subprocess.run([nginx, "-t", "-p", str(base) + "/", "-c", str(config)],
                       check=True, timeout=15)
        print("PASS isolated nginx site syntax including both old Wikipedia and new ZIM reader")


if __name__ == "__main__":
    main()
