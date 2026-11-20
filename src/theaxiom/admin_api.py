#!/usr/bin/env python3
"""Offline library admin API and trusted-LAN development server."""

from __future__ import annotations

import json
import importlib
import os
import re
import subprocess
import threading
from contextlib import ExitStack
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

if __package__:
    from .freshness_probe import probe_source
else:
    from freshness_probe import probe_source
storage_settings = importlib.import_module(
    f"{__package__}.storage_settings" if __package__ else "storage_settings"
)

storage = importlib.import_module(f"{__package__}.storage" if __package__ else "theaxiom.storage")
catalog_store = importlib.import_module(f"{__package__}.catalog_store" if __package__ else "theaxiom.catalog_store")

security = importlib.import_module(f"{__package__}.admin_security" if __package__ else "theaxiom.admin_security")
monitoring = importlib.import_module(f"{__package__}.acquisition_monitor" if __package__ else "theaxiom.acquisition_monitor")
publication = importlib.import_module(f"{__package__}.publication" if __package__ else "theaxiom.publication")
pdf_source = importlib.import_module(f"{__package__}.pdf_source" if __package__ else "theaxiom.pdf_source")
pdf_transport = importlib.import_module(f"{__package__}.pdf_transport" if __package__ else "theaxiom.pdf_transport")
library_search = importlib.import_module(f"{__package__}.search" if __package__ else "theaxiom.search")
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080
ADMIN_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

ACTION_LABELS = {"download": "Download", "redownload": "Re-download", "update": "Update"}
ACQUISITION_DISABLED = "acquisition disabled pending storage safety integration"

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


def _release_revision() -> str | None:
    """Return the immutable source revision loaded by this process, if known."""
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None


ARTIFACT_FIELDS = {"format", "source_url", "expected_size_bytes", "sha256"}
FORMAT_ADAPTERS = {"zim": "kiwix", "pdf": "pdf"}


def select_lifecycle(source_id, configuration):
    """Select by format first; candidate configuration never creates trust."""
    format_name = configuration.get("format")
    adapter = FORMAT_ADAPTERS.get(format_name) if isinstance(format_name, str) else None
    if adapter == "pdf" and source_id not in publication.ROUTES:
        artifact = None
        try:
            artifact = pdf_source.pin(source_id, configuration)
            reason = "Import from the local PDF inbox or download the configured HTTPS document."
        except ValueError as exc:
            reason = str(exc)
        return dict(adapter=adapter, configuration_valid=artifact is not None, artifact=artifact,
                    pdf_download=pdf_source.valid_source_url(configuration.get("source_url", "")),
                    acquisition_reason=reason, actions={})
    # CatalogStore validates field syntax; the adapter also requires these
    # fields to be present before a candidate can select an acquisition pin.
    valid = bool(adapter and all(isinstance(configuration.get(key), str) and configuration[key]
                                 for key in ARTIFACT_FIELDS))
    artifact = None
    if not adapter:
        reason = "No lifecycle adapter supports this format."
    elif not valid:
        reason = "ZIM candidate configuration requires a URL, expected size and SHA-256."
    else:
        entry = resolve_source(source_id)
        if entry is None:
            reason = "No trusted acquisition manifest is configured for this source."
        elif (configuration["format"] != entry["kind"]
              or configuration["source_url"] != entry["source_url"]
              or configuration["expected_size_bytes"] != str(entry["bytes"])
              or configuration["sha256"].lower() != entry["sha256"]):
            reason = "Candidate configuration does not match the trusted acquisition manifest."
        else:
            artifact = {key: entry[key] for key in ("filename", "source_url", "bytes", "sha256")}
            reason = ACQUISITION_DISABLED
    return {"adapter": adapter, "configuration_valid": valid, "artifact": artifact,
            "acquisition_reason": reason,
            "actions": {action: False for action in ACTION_LABELS} if artifact else {}}


RELEASE_REVISION = _release_revision()


def health_response() -> dict[str, Any]:
    """Return liveness and immutable source revision loaded by this process."""
    return {
        "status": "ok",
        "service": "offline-library-admin-api",
        "revision": RELEASE_REVISION,
    }


def freshness_response() -> dict[str, Any]:
    """Return origin freshness metadata for the trusted source manifest."""
    entry = WIKIPEDIA_SOURCE
    result = probe_source(entry["source_url"], entry["sha256"], entry["bytes"])
    result.update(source_id=entry["source_id"], title=entry["title"])
    return result


# Local development defaults follow the XDG user data location. Deployments can
# override both values explicitly without depending on this source repository.
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SOURCE_CATALOG_PATH = Path(os.environ.get("THEAXIOM_SOURCE_CATALOG", Path(storage_settings.SETTINGS_PATH or CONFIG_HOME / "theaxiom/storage.json").with_name("sources.json")))
SOURCE_CATALOG = catalog_store.CatalogStore(SOURCE_CATALOG_PATH)
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


def source_busy(snapshot, source_id):
    if snapshot["stale"] or any(
        job["source_id"] == source_id and (job.get("active") is not False
        or job.get("state") not in ("completed", "not_started", "unavailable"))
        for job in snapshot["acquisitions"]
    ):
        return True
    with _active_downloads_lock:
        return any(job["source_id"] == source_id and job["process"].poll() is None
                   for job in _jobs.values())


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
        public_paths = {"/", "/index.html", "/health", "/health/", "/internal/publication/wikipedia-en", "/read/wikipedia-en"}
        if not post and (self.path in public_paths or self.path.split("?", 1)[0] == "/search"
                         or pdf_source.ROUTE.fullmatch(self.path)):
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
        pdf_route = pdf_source.ROUTE.fullmatch(self.path)
        if pdf_route:
            self._serve_pdf(*pdf_route.groups())
            return
        if self.path in {"/", "/index.html"}:
            sources = []
            snapshot = self._publication_snapshot()
            for source_id, route in publication.ROUTES.items():
                if publication.status(snapshot, source_id)["published"]:
                    sources.append(route)
            for record, state in self._pdf_publications():
                if state["published"]:
                    profile = record["profile"]
                    sources.append(dict(entry_path=state["entry_path"], symbol="P", category="PDF",
                                        name=profile["title"], description=profile.get("description", ""),
                                        language=profile.get("language") or "Unspecified language"))
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            body = environment.get_template("library.html.j2").render(sources=sources).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.split("?", 1)[0] == "/search":
            query, groups, error, code = "", [], "", 200
            try:
                if len(self.path) > 4096:
                    raise ValueError("Search request too long")
                params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                                  max_num_fields=2, errors="strict")
                if set(params) - {"q"} or len(params.get("q", [""])) != 1:
                    raise ValueError("Invalid search request")
                query = library_search.validate_query(params.get("q", [""])[0])
                groups = library_search.search(query, self._publication_snapshot)
            except (ValueError, UnicodeError):
                error, code = "Invalid search query.", 400
            except RuntimeError:
                error, code = "Search is busy. Try again.", 503
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            body = environment.get_template("search.html.j2").render(
                query=query, groups=groups, error=error).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/read/wikipedia-en":
            source_id = "wikipedia-en"
            if not publication.allowed(self._publication_snapshot(), source_id):
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
            self.send_response(204 if publication.allowed(self._publication_snapshot(), "wikipedia-en") else 403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/api/publication":
            snapshot = self._publication_snapshot()
            self._send_json(200, {key: publication.status(snapshot, key)
                                  for key in publication.ROUTES}
                            | {item["source_id"]: state for item, state in self._pdf_publications()})
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
        elif self.path.rstrip("/") == "/api/sources":
            try:
                self._send_json(200, self._source_profiles(SOURCE_CATALOG.get()))
            except (catalog_store.CatalogError, OSError):
                self._send_json(503, {"error": "source catalog unavailable; stored data was not replaced"})
        elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", self.path.rstrip("/")):
            job_id = self.path.rstrip("/").rsplit("/", 1)[-1]
            response = job_status_response(job_id)
            if response is None:
                self._send_json(404, {"error": "job not found"})
            else:
                self._send_json(200, response)
        else:
            self._send_json(404, {"error": "not found"})

    @staticmethod
    def _pdf_record(record):
        return (record["source_id"] not in publication.ROUTES
                and record["configuration"].get("format") == "pdf")

    def _pdf_publications(self):
        try:
            catalog = SOURCE_CATALOG.get()
        except (catalog_store.CatalogError, OSError):
            return
        for record in catalog["sources"]:
            if self._pdf_record(record):
                artifact = select_lifecycle(record["source_id"], record["configuration"])["artifact"]
                yield record, pdf_source.status(record, artifact)

    def _serve_pdf(self, sid, document):
        with ExitStack() as files:
            try:
                with publication.LOCK:
                    if not publication.load().get(sid):
                        raise ValueError
                    record = next((item for item in SOURCE_CATALOG.get()["sources"]
                                   if item["source_id"] == sid and self._pdf_record(item)), None)
                    if record is None:
                        raise ValueError
                    artifact = pdf_source.pin(sid, record["configuration"])
                    stream = files.enter_context(pdf_source.installed(artifact, verify=True))
            except (OSError, ValueError):
                self._send_json(403, {"error": "document unavailable"})
                return
            if not document:
                environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
                body = environment.get_template("pdf-reader.html.j2").render(
                    record=record, path=self.path + "/document.pdf").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{sid}.pdf"')
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.send_header("Content-Length", str(artifact["bytes"]))
            self.end_headers()
            remaining = artifact["bytes"]
            while remaining and (block := stream.read(min(remaining, pdf_source.CHUNK))):
                self.wfile.write(block)
                remaining -= len(block)

    def _snapshot(self):
        monitor = getattr(self.server, "acquisition_monitor", None)
        try:
            if monitor:
                snap = monitor.snapshot()
                jobs = snap["acquisitions"]
                if (type(snap["stale"]) is bool and isinstance(jobs, list)
                        and all(isinstance(job, dict) and isinstance(job.get("source_id"), str)
                                for job in jobs)):
                    return snap
        except Exception:
            pass
        return {"acquisitions": [], "stale": True}

    @staticmethod
    def _matched_snapshot(snapshot, artifacts):
        jobs = []
        for job in snapshot["acquisitions"]:
            artifact = artifacts.get(job["source_id"])
            if artifact and all(job.get(field) == artifact[key] for field, key in (
                    ("filename", "filename"), ("expected_bytes", "bytes"), ("sha256", "sha256"))):
                jobs.append(job)
        return dict(snapshot, acquisitions=jobs)

    @staticmethod
    def _lifecycles(catalog):
        return {record["source_id"]: select_lifecycle(record["source_id"], record["configuration"])
                for record in catalog["sources"]}

    def _publication_snapshot(self):
        try:
            lifecycles = self._lifecycles(SOURCE_CATALOG.get())
            return self._matched_snapshot(self._snapshot(), {sid: life["artifact"] for sid, life in lifecycles.items()})
        except (catalog_store.CatalogError, OSError):
            return {"acquisitions": [], "stale": True}

    def _source_profiles(self, catalog):
        snapshot = self._snapshot()
        lifecycles = self._lifecycles(catalog)
        artifacts = {sid: life["artifact"] for sid, life in lifecycles.items()}
        matched = self._matched_snapshot(snapshot, artifacts)
        jobs = {job["source_id"]: job for job in matched["acquisitions"]}
        sources = []
        for record in catalog["sources"]:
            sid = record["source_id"]
            busy = source_busy(snapshot, sid)
            state = None
            if sid in publication.ROUTES:
                state = publication.status(matched, sid)
                state["entry_path"] = publication.ROUTES[sid]["entry_path"]
                if not artifacts[sid] and not state["enabled"] and not state.get("error"):
                    state = None
            elif self._pdf_record(record):
                busy = False
                pdf_state = pdf_source.status(record, artifacts[sid])
                if artifacts[sid] or pdf_state["enabled"] or pdf_state.get("error"):
                    state = pdf_state
            sources.append(dict(record, **lifecycles[sid], acquisition=jobs.get(sid),
                                installed_verified=(bool(state and state["verified"]) if self._pdf_record(record)
                                                    else publication.verified(matched, sid)),
                                publication=state, mutation_blocked=busy))
        return dict(catalog, sources=sources, acquisition_stale=snapshot["stale"])

    def _send_admin_page(self, page: str = "sources") -> None:
        """Render exactly the persisted source profiles."""
        environment = Environment(
            loader=FileSystemLoader(ADMIN_TEMPLATE_DIR),
            autoescape=True,
        )
        template = environment.get_template("admin-page.html.j2")
        try:
            catalog_snapshot = SOURCE_CATALOG.get() if page == "sources" else {"revision": None, "sources": []}
        except (catalog_store.CatalogError, OSError):
            self._send_json(503, {"error": "source catalog unavailable; stored data was not replaced"})
            return
        body = template.render(
            admin_page=page,
            catalog=self._source_profiles(catalog_snapshot) if page == "sources" else None,
            editable_fields=sorted(catalog_store.EDITABLE),
            action_labels=ACTION_LABELS,
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
        elif self.path.rstrip("/") == "/api/sources":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            if set(data) != {"operation", "source_id", "fields", "revision"}:
                self._send_json(400, {"error": "invalid source mutation"})
                return
            sid = data.get("source_id")
            try:
                with publication.LOCK:
                    operation, fields, revision = data["operation"], data["fields"], data["revision"]
                    existing = next((item for item in SOURCE_CATALOG.get()["sources"] if item["source_id"] == sid), None)
                    is_pdf = (existing and self._pdf_record(existing)) or (
                        existing is None and isinstance(sid, str) and sid not in publication.ROUTES
                        and isinstance(fields, dict) and fields.get("format") == "pdf")
                    if isinstance(sid, str) and (sid in publication.ROUTES or is_pdf):
                        self._guard_published_mutation(operation, sid, fields, revision)
                    result = SOURCE_CATALOG.mutate(operation, sid, fields, revision,
                                                   not is_pdf and source_busy(self._snapshot(), sid))
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "source catalog write failed"})
                return
            self._send_json(200, self._source_profiles(result))
        elif self.path == "/api/publication":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            source_id, enabled = data.get("source_id"), data.get("enabled")
            if (set(data) != {"source_id", "enabled", "revision"} or not isinstance(source_id, str)
                    or not catalog_store.ID_RE.fullmatch(source_id) or type(enabled) is not bool
                    or not self._valid_revision(data["revision"])):
                self._send_json(400, {"error": "invalid publication request"})
                return
            with publication.LOCK:
                try:
                    catalog = SOURCE_CATALOG.get()
                except (catalog_store.CatalogError, OSError):
                    self._send_json(503, {"error": "source catalog unavailable"})
                    return
                if data["revision"] != catalog["revision"]:
                    self._send_json(409, {"error": "catalog revision changed"})
                    return
                record = next((item for item in catalog["sources"] if item["source_id"] == source_id), None)
                is_pdf = record is not None and self._pdf_record(record)
                if source_id not in publication.ROUTES and not is_pdf:
                    self._send_json(400, {"error": "source has no publication adapter"})
                    return
                if enabled and record is not None and is_pdf:
                    artifact = select_lifecycle(source_id, record["configuration"])["artifact"]
                    if not pdf_source.ready(artifact, verify=True):
                        self._send_json(409, {"error": "matching verified PDF content is required"})
                        return
                elif enabled:
                    snapshot = self._snapshot()
                    life = self._lifecycles(catalog).get(source_id)
                    artifact = life["artifact"] if life else None
                    matched = self._matched_snapshot(snapshot, {source_id: artifact})
                    if source_busy(snapshot, source_id) or not publication.status(matched, source_id)["serving"]:
                        self._send_json(409, {"error": "matching source configuration, verified content and a reachable reader are required"})
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
                with publication.LOCK:
                    settings = storage_settings.save_settings(data)
            except (ValueError, OSError):
                self._send_json(400, {"error": "invalid storage settings"})
                return
            self._send_json(200, settings)
        elif self.path.rstrip("/") == "/api/action":
            data = self._read_body()
            if data.get("action") == "import":
                self._import_pdf(data)
            elif data.get("action") == "pdf-download":
                self._download_pdf(data)
            else:
                self._refuse_acquisition(data)
        else:
            self._send_json(404, {"error": "not found"})

    @staticmethod
    def _valid_revision(revision):
        return type(revision) is int and revision >= 0

    def _import_pdf(self, data):
        sid = data.get("source_id")
        if (set(data) != {"source_id", "action", "revision"} or not isinstance(sid, str)
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "import requires a source ID and catalog revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if record is None or not self._pdf_record(record):
                    raise ValueError("source has no PDF import adapter")
                artifact = pdf_source.pin(sid, record["configuration"])
                pdf_source.import_local(sid, artifact)
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "PDF inbox or local storage unavailable"})
                return
        self._send_json(200, {"accepted": True, "installed_verified": True})

    def _download_pdf(self, data):
        sid = data.get("source_id")
        if (set(data) != {"source_id", "action", "revision"} or not isinstance(sid, str)
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "download requires a source ID and catalog revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if record is None or not self._pdf_record(record):
                    raise ValueError("source has no PDF download adapter")
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish source before downloading")
                config = record["configuration"]
                url = config.get("source_url", "")
                if not pdf_source.valid_source_url(url):
                    raise ValueError("A direct HTTPS PDF URL is required")
                size = config.get("expected_size_bytes") or None
                digest = config.get("sha256") or None
                observed = pdf_transport.download(sid, url, expected_size=int(size) if size else None,
                                                  expected_sha256=digest)
                provenance = config.get("provenance", "").strip()
                if not provenance:
                    provenance = "Downloaded via configured HTTPS URL; SHA-256 calculated locally, not independently publisher-verified."
                elif not digest:
                    suffix = " SHA-256 calculated locally, not independently publisher-verified."
                    provenance = provenance[:catalog_store.MAX_FIELD - len(suffix)] + suffix
                fields = {"expected_size_bytes": str(observed["bytes"]), "sha256": observed["sha256"],
                          "provenance": provenance}
                updated = SOURCE_CATALOG.mutate("edit", sid, fields, data["revision"])
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except FileNotFoundError:
                self._send_json(503, {"error": "PDF storage parent directory or catalog path is missing"})
                return
            except PermissionError:
                self._send_json(503, {"error": "PDF storage or catalog is not writable by the application"})
                return
            except OSError:
                self._send_json(503, {"error": "PDF storage or catalog I/O failed"})
                return
        self._send_json(200, {"accepted": True, "installed_verified": True, "revision": updated["revision"]})

    def _refuse_acquisition(self, data):
        def refuse(status, error):
            self._send_json(status, {"accepted": False, "error": error})

        sid, action = data.get("source_id"), data.get("action")
        if not isinstance(sid, str) or not catalog_store.ID_RE.fullmatch(sid):
            return refuse(400, "invalid source_id")
        if not isinstance(action, str) or action not in ACTION_LABELS:
            return refuse(400, "unknown action")
        if set(data) != {"source_id", "action", "revision"} or not self._valid_revision(data["revision"]):
            return refuse(400, "action requires a catalog revision and accepts no acquisition metadata")
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                return refuse(503, "source catalog unavailable")
            if data["revision"] != catalog["revision"]:
                return refuse(409, "catalog revision changed")
            record = next((record for record in catalog["sources"] if record["source_id"] == sid), None)
            if record is None:
                return refuse(400, "unknown source_id: no configured profile")
            life = select_lifecycle(sid, record["configuration"])
            if not life["artifact"]:
                return refuse(409, life["acquisition_reason"])
            snapshot = self._snapshot()
            matching = self._matched_snapshot(snapshot, {sid: life["artifact"]})
            if (source_busy(snapshot, sid) or (any(job["source_id"] == sid for job in snapshot["acquisitions"])
                                               and not matching["acquisitions"])):
                return refuse(409, "source has active or unresolved acquisition")
            # No admin-triggered acquisition is implemented, even for a trusted pin.
            return refuse(503, life["acquisition_reason"])

    @staticmethod
    def _guard_published_mutation(operation, sid, fields, revision):
        if operation not in ("remove", "edit", "add"):
            return
        try:
            enabled = publication.load().get(sid, False)
        except (OSError, ValueError) as exc:
            raise catalog_store.RevisionConflict("publication state unavailable; source mutation refused") from exc
        if not enabled:
            return
        if operation in ("remove", "add"):
            raise catalog_store.RevisionConflict("unpublish source before removing or re-adding it")
        guarded_fields = ARTIFACT_FIELDS if sid in publication.ROUTES else catalog_store.CONFIG_FIELDS
        if isinstance(fields, dict) and fields.keys() & guarded_fields:
            catalog = SOURCE_CATALOG.get()
            if catalog["revision"] != revision:
                raise catalog_store.RevisionConflict("catalog revision changed")
            record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
            if record:
                config = record["configuration"]
                proposed = config | fields
                if ((sid in publication.ROUTES and not select_lifecycle(sid, proposed)["artifact"])
                        or (sid not in publication.ROUTES and any(proposed.get(k) != config.get(k) for k in guarded_fields))):
                    raise catalog_store.RevisionConflict("unpublish source before changing artifact configuration")

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
