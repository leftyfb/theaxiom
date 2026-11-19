#!/usr/bin/env python3
"""Offline library admin API and trusted-LAN development server."""

from __future__ import annotations

import json
import importlib
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if __package__:
    from .freshness_probe import probe_source
else:
    from freshness_probe import probe_source
storage_settings = importlib.import_module(
    f"{__package__}.storage_settings" if __package__ else "storage_settings"
)

storage = importlib.import_module(f"{__package__}.storage" if __package__ else "theaxiom.storage")

security = importlib.import_module(f"{__package__}.admin_security" if __package__ else "theaxiom.admin_security")
monitoring = importlib.import_module(f"{__package__}.acquisition_monitor" if __package__ else "theaxiom.acquisition_monitor")
publication = importlib.import_module(f"{__package__}.publication" if __package__ else "theaxiom.publication")
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080
ADMIN_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

VALID_ACTIONS = frozenset({"download", "redownload", "update"})
VALID_SOURCE_IDS = frozenset({"wikipedia-en"})

# Trusted acquisition metadata is shipped separately from planned catalog entries.
source_catalog = importlib.import_module(f"{__package__}.source_catalog" if __package__ else "theaxiom.source_catalog")
WIKIPEDIA_SOURCE = source_catalog.WIKIPEDIA_SOURCE

# Map of every resolvable source_id -> its trusted manifest entry.
TRUSTED_SOURCES: dict[str, dict[str, Any]] = {
    WIKIPEDIA_SOURCE["source_id"]: WIKIPEDIA_SOURCE,
}


def resolve_source(source_id: str) -> dict[str, Any] | None:
    """Resolve a source_id to its trusted manifest entry.

    Returns None for unknown or untrusted source IDs. Never accepts
    client-supplied URLs, commands, or checksums.
    """
    return TRUSTED_SOURCES.get(source_id)


def health_response() -> dict[str, Any]:
    """Return the fixed liveness payload."""
    return {"status": "ok", "service": "offline-library-admin-api"}


def freshness_response() -> dict[str, Any]:
    """Return origin freshness metadata for the trusted source manifest."""
    entry = WIKIPEDIA_SOURCE
    result = probe_source(entry["source_url"], entry["sha256"], entry["bytes"])
    result.update(source_id=entry["source_id"], title=entry["title"])
    return result


# Local development defaults follow the XDG user data location. Deployments can
# override both values explicitly without depending on this source repository.
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
ACQUISITION_STAGING_DIR = Path(
    os.environ.get("THEAXIOM_STAGING_DIR", DATA_HOME / "theaxiom/staging")
)
ACQUISITION_SCRIPT = Path(
    os.environ.get(
        "THEAXIOM_ACQUISITION_SCRIPT",
        Path(__file__).resolve().parents[2] / "scripts/acquire_kiwix_zim.sh",
    )
)

# Keep process handles so completed jobs can be distinguished from active ones.
_active_downloads: dict[str, subprocess.Popen[bytes]] = {}
_jobs: dict[str, dict[str, Any]] = {}
_active_downloads_lock = threading.Lock()
MAX_RETAINED_JOBS = 100


def job_status_response(job_id: str) -> dict[str, Any] | None:
    """Return a bounded status record for a retained acquisition job."""
    with _active_downloads_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        process = job["process"]
        return {
            "job_id": job_id,
            "source_id": job["source_id"],
            "action": job["action"],
            "status": "pending" if process.poll() is None else (
                "completed" if process.returncode == 0 else "failed"
            ),
            "returncode": process.returncode,
        }


def build_acquisition_args(source_id: str) -> list[str] | None:
    """Construct the acquire_kiwix_zim.sh argument list from trusted config.

    The acquisition script (scripts/acquire_kiwix_zim.sh) expects four
    positional arguments: URL, destination, expected bytes, expected SHA-256.
    All values are sourced from the trusted manifest entry for source_id;
    no client-supplied data is used.

    Returns None if source_id is unknown or untrusted.
    """
    entry = resolve_source(source_id)
    if entry is None:
        return None
    destination = f"{ACQUISITION_STAGING_DIR}/{entry['filename']}"
    return [
        entry["source_url"],
        destination,
        str(entry["bytes"]),
        entry["sha256"],
    ]


def action_response(source_id: str, action: str) -> dict[str, Any]:
    """Fail closed until verified storage and safe acquisition are integrated."""
    if resolve_source(source_id) is None:
        return {"accepted": False, "error": "unknown source_id"}
    if action not in VALID_ACTIONS:
        return {"accepted": False, "error": "unknown action"}
    return {"accepted": False, "error": "acquisition disabled pending storage safety integration"}


class AdminAPIHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for the admin API."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _guard(self, *, post: bool = False) -> bool:
        # The plain server used by legacy local tests must never authorize
        # an unauthenticated non-loopback listener.
        config = getattr(self.server, "axiom_config", None)
        if config is None:
            host, port = self.server.server_address[:2]
            config = security.ServerConfig(host, port, frozenset({
                f"{host}:{port}", f"localhost:{port}"
            }))
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in config.allowed_hosts:
            self._send_json(400, {"error": "invalid host"})
            return False
        if not post and self.path in {"/", "/index.html", "/health", "/health/", "/internal/publication/wikipedia-en", "/read/wikipedia-en"}:
            return True
        auth = self.headers.get_all("Authorization", [])
        local_hosts = {f"{config.host}:{config.port}", f"localhost:{config.port}"}
        if config.port == 80:
            local_hosts.update({config.host, "localhost"})
        if (len(auth) > 1 or (config.store is None and hosts[0].lower() not in local_hosts)
                or not config.authorized(auth[0] if auth else None)):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="The Axiom", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if post:
            origins = self.headers.get_all("Origin", [])
            scheme = self.headers.get("X-Forwarded-Proto", "") if config.proxied else "http"
            if scheme not in {"http", "https"}:
                self._send_json(400, {"error": "invalid proxy scheme"})
                return False
            if (len(origins) > 1 or
                (origins and origins[0] != scheme + "://" + hosts[0].lower()) or
                self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
                self._send_json(403, {"error": "cross-origin request rejected"})
                return False
            if self.headers.get_content_type() != "application/json":
                self._send_json(415, {"error": "application/json required"})
                return False
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]) or
                    int(lengths[0]) > 16384 or self.headers.get("Transfer-Encoding")):
                self._send_json(400, {"error": "invalid request length"})
                return False
        return True

    def setup(self) -> None:
        self.request.settimeout(15)
        super().setup()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - http.server convention
        if not self._guard():
            return
        if self.path in {"/", "/index.html"}:
            sources = []
            for source_id, route in publication.ROUTES.items():
                if publication.status(self._snapshot(), source_id)["published"]:
                    sources.append(route)
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            body = environment.get_template("library.html.j2").render(sources=sources).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/read/wikipedia-en":
            source_id = "wikipedia-en"
            if not publication.allowed(self._snapshot(), source_id):
                self._send_json(403, {"error": "collection unavailable"})
                return
            location = publication.reader_location(publication.ROUTES[source_id])
            if location is None:
                self._send_json(503, {"error": "reader entry unavailable"})
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/internal/publication/wikipedia-en":
            self.send_response(204 if publication.allowed(self._snapshot(), "wikipedia-en") else 403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/api/publication":
            self._send_json(200, {key: publication.status(self._snapshot(), key)
                                  for key in publication.ROUTES})
        elif self.path.rstrip("/") == "/health":
            self._send_json(200, health_response())
        elif self.path.rstrip("/") in {"/admin", "/admin/sources", "/admin/settings"}:
            page = "settings" if self.path.rstrip("/") == "/admin/settings" else "sources"
            self._send_admin_page(page)
        elif self.path.rstrip("/") == "/api/freshness":
            self._send_json(200, freshness_response())
        elif self.path.rstrip("/") == "/api/acquisitions":
            monitor = getattr(self.server, "acquisition_monitor", None)
            self._send_json(200, monitor.snapshot() if monitor else {"acquisitions": [], "stale": True})
        elif self.path.rstrip("/") == "/api/storage-status":
            try:
                self._send_json(200, storage.inspect_storage(storage_settings.load_settings(), required_bytes=WIKIPEDIA_SOURCE["bytes"]))
            except ValueError:
                self._send_json(503, {"ready": False, "error": "stored settings are invalid"})
        elif self.path.rstrip("/") == "/api/storage-settings":
            try:
                self._send_json(200, storage_settings.load_settings())
            except ValueError:
                self._send_json(500, {"error": "stored settings are invalid"})
        elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", self.path.rstrip("/")):
            job_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            response = job_status_response(job_id)
            if response is None:
                self._send_json(404, {"error": "job not found"})
            else:
                self._send_json(200, response)
        else:
            self._send_json(404, {"error": "not found"})

    def _snapshot(self):
        monitor = getattr(self.server, "acquisition_monitor", None)
        return monitor.snapshot() if monitor else {"acquisitions": [], "stale": True}

    def _send_admin_page(self, page: str = "sources") -> None:
        """Render the standalone admin page using trusted source metadata."""
        environment = Environment(
            loader=FileSystemLoader(ADMIN_TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "htm", "xml"]),
        )
        template = environment.get_template("admin-page.html.j2")
        body = template.render(
            admin_page=page,
            offline_library_content_manifest=source_catalog.planned_sources() if page == "sources" else [],
            offline_library_kiwix_zim_enabled=True,
            offline_library_kiwix_zim=WIKIPEDIA_SOURCE,
            password_change_enabled=bool(getattr(getattr(self.server, "axiom_config", None), "store", None)),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server convention
        if not self._guard(post=True):
            return
        if self.path.rstrip("/") == "/api/admin/password":
            config = getattr(self.server, "axiom_config", None)
            if config is None or config.store is None:
                self._send_json(503, {"error": "admin authentication is not configured"})
                return
            # A browser password change must explicitly originate from this page.
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if (set(data) != {"current_password", "new_password", "confirm_password"}
                    or any(not isinstance(value, str) for value in data.values())
                    or data["new_password"] != data["confirm_password"]):
                self._send_json(400, {"error": "password confirmation does not match"})
                return
            try:
                changed = config.store.change(data["current_password"], data["new_password"])
            except ValueError:
                self._send_json(400, {"error": "choose a different password of 8–256 printable non-space ASCII characters"})
                return
            except OSError:
                self._send_json(503, {"error": "credential update failed"})
                return
            if not changed:
                self._send_json(403, {"error": "current password is incorrect"})
                return
            self._send_json(200, {"changed": True})
        elif self.path == "/api/publication":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            source_id, enabled = data.get("source_id"), data.get("enabled")
            if (set(data) != {"source_id", "enabled"} or not isinstance(source_id, str)
                    or source_id not in publication.ROUTES or type(enabled) is not bool):
                self._send_json(400, {"error": "invalid publication request"})
                return
            with publication.LOCK:
                state = publication.status(self._snapshot(), source_id, probe=enabled)
                if enabled and not state["serving"]:
                    self._send_json(409, {"error": "verified content and a reachable reader are required"})
                    return
                try:
                    publication.save(source_id, enabled)
                except (OSError, ValueError):
                    self._send_json(503, {"error": "could not save publication state"})
                    return
            self._send_json(200, {"enabled": enabled})
        elif self.path.rstrip("/") == "/api/storage-settings":
            data = self._read_body()
            try:
                settings = storage_settings.save_settings(data)
            except (ValueError, OSError):
                self._send_json(400, {"error": "invalid storage settings"})
                return
            self._send_json(200, settings)
        elif self.path.rstrip("/") == "/api/action":
            data = self._read_body()
            source_id = str(data.get("source_id", "")).strip()
            action = str(data.get("action", "")).strip()
            if not source_id or not action:
                self._send_json(400, {"error": "source_id and action are required"})
                return
            if source_id not in VALID_SOURCE_IDS:
                self._send_json(400, {"error": "unknown source_id"})
                return
            if action not in VALID_ACTIONS:
                self._send_json(400, {"error": "unknown action"})
                return
            response = action_response(source_id, action)
            self._send_json(200 if response["accepted"] else 503, response)
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default per-request stderr logging; structured logging
        # is handled by the systemd unit.
        return


def main() -> None:
    """Start the admin API server."""
    config = security.load_config()
    server = ThreadingHTTPServer((config.host, config.port), AdminAPIHandler)
    server.axiom_config = config
    server.acquisition_monitor = monitoring.Monitor(WIKIPEDIA_SOURCE)
    server.acquisition_monitor.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.acquisition_monitor.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
