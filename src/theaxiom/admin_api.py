#!/usr/bin/env python3
"""Offline library admin API backend.

A small standard-library HTTP server that exposes bounded admin endpoints
for the offline library WebUI. Listens on localhost only so nginx proxies
the authenticated /admin route to it.

Endpoints (Phase B):
  GET  /health      — fixed JSON liveness response
  POST /api/action  — launch a trusted acquisition for a source ID
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if __package__:
    from .freshness_probe import probe_source
else:
    from freshness_probe import probe_source

BIND_HOST = "127.0.0.1"
BIND_PORT = 8080
ADMIN_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

VALID_ACTIONS = frozenset({"download", "redownload", "update"})
VALID_SOURCE_IDS = frozenset({"wikipedia-en"})

# Trusted manifest — canonical Wikipedia ZIM source. Values are pinned in this
# application module until catalog loading is implemented; clients cannot supply
# URLs or commands.
WIKIPEDIA_SOURCE: dict[str, Any] = {
    "source_id": "wikipedia-en",
    "title": "Wikipedia English All Maxi (2026-08)",
    "kind": "zim",
    "filename": "wikipedia_en_all_maxi_2026-08.zim",
    "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim",
    "sha256": "34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6",
    "bytes": 127418087648,
    "language": "eng",
    "publisher": "Wikimedia contributors via Kiwix",
    "catalog_date": "2026-07-06",
    "license": "CC BY-SA 4.0 and GFDL for Wikipedia text; individual media licenses may vary",
    "license_url": "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
    "attribution": "Preserve article-level attribution, applicable license notices, and modification notices.",
    "rights_review_status": "reviewed-for-whole-zim-redistribution",
}

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
    """Skip current sources; otherwise launch trusted acquisition."""
    args = build_acquisition_args(source_id)
    if args is None:
        return {"accepted": False, "error": "unknown source_id"}
    if action != "redownload":
        entry = resolve_source(source_id)
        assert entry is not None
        freshness = probe_source(entry["source_url"], entry["sha256"], entry["bytes"])
        if freshness.get("local_current"):
            return {
                "accepted": True,
                "source_id": source_id,
                "action": action,
                "status": "current",
                "skipped": True,
            }
    with _active_downloads_lock:
        process = _active_downloads.get(source_id)
        if process is not None and process.poll() is None:
            return {"accepted": False, "error": "download already active for source_id"}
        _active_downloads.pop(source_id, None)
        job_id = uuid.uuid4().hex
        try:
            command = ["/bin/bash", ACQUISITION_SCRIPT, *args]
            if action == "redownload":
                command.append("--overwrite")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return {"accepted": False, "error": f"acquisition launch rejected: {exc}"}
        _active_downloads[source_id] = process
        _jobs[job_id] = {"source_id": source_id, "action": action, "process": process}
        while len(_jobs) > MAX_RETAINED_JOBS:
            _jobs.pop(next(iter(_jobs)))
    return {
        "accepted": True,
        "job_id": job_id,
        "source_id": source_id,
        "action": action,
        "status": "pending",
    }


class AdminAPIHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for the admin API."""

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
        if self.path.rstrip("/") == "/health":
            self._send_json(200, health_response())
        elif self.path.rstrip("/") == "/admin":
            self._send_admin_page()
        elif self.path.rstrip("/") == "/api/freshness":
            self._send_json(200, freshness_response())
        elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", self.path.rstrip("/")):
            job_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            response = job_status_response(job_id)
            if response is None:
                self._send_json(404, {"error": "job not found"})
            else:
                self._send_json(200, response)
        else:
            self._send_json(404, {"error": "not found"})

    def _send_admin_page(self) -> None:
        """Render the standalone admin page using trusted source metadata."""
        environment = Environment(
            loader=FileSystemLoader(ADMIN_TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "htm", "xml"]),
        )
        template = environment.get_template("admin-page.html.j2")
        body = template.render(
            offline_library_content_manifest=[],
            offline_library_kiwix_zim_enabled=True,
            offline_library_kiwix_zim=WIKIPEDIA_SOURCE,
            offline_library_storage_mount=str(ACQUISITION_STAGING_DIR.parent),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server convention
        if self.path.rstrip("/") == "/api/action":
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
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), AdminAPIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
