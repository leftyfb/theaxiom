#!/usr/bin/env python3
"""Offline library admin API and trusted-LAN development server."""

from __future__ import annotations

import json
from io import BytesIO
import importlib
import os
import re
import subprocess
import threading
from contextlib import ExitStack
from urllib.parse import parse_qs, urlsplit, unquote
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
admin_sessions = importlib.import_module(f"{__package__}.admin_sessions" if __package__ else "theaxiom.admin_sessions")
monitoring = importlib.import_module(f"{__package__}.acquisition_monitor" if __package__ else "theaxiom.acquisition_monitor")
publication = importlib.import_module(f"{__package__}.publication" if __package__ else "theaxiom.publication")
pdf_source = importlib.import_module(f"{__package__}.pdf_source" if __package__ else "theaxiom.pdf_source")
epub_source = importlib.import_module(f"{__package__}.epub_source" if __package__ else "theaxiom.epub_source")
pdf_language = importlib.import_module(f"{__package__}.pdf_language" if __package__ else "theaxiom.pdf_language")
pdf_transport = importlib.import_module(f"{__package__}.pdf_transport" if __package__ else "theaxiom.pdf_transport")
zim_jobs = importlib.import_module(f"{__package__}.zim_jobs" if __package__ else "theaxiom.zim_jobs")
zim_source = importlib.import_module(f"{__package__}.zim_source" if __package__ else "theaxiom.zim_source")
zim_catalog = importlib.import_module(f"{__package__}.zim_catalog" if __package__ else "theaxiom.zim_catalog")
zim_refresh = importlib.import_module(f"{__package__}.zim_refresh" if __package__ else "theaxiom.zim_refresh")
library_search = importlib.import_module(f"{__package__}.search" if __package__ else "theaxiom.search")
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080
ADMIN_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
PUBLIC_ICON_PATHS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}
PUBLIC_ICON_DIR = ADMIN_TEMPLATE_DIR
ADMIN_SESSIONS = admin_sessions.Sessions()
PDFJS_ROOT = Path("/opt/pdfjs/current")
PDFJS_PREFIX = "/api/pdfjs/"
ZIM_READER_ROUTE = re.compile(r'/read/zim/([a-z0-9-]{1,63})\Z')
PDFJS_ASSET = re.compile(r"/api/pdfjs/(?:build|web|cmaps|standard_fonts|wasm|iccs|image_decoders|locale)/[A-Za-z0-9_./-]+\Z")

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
FORMAT_ADAPTERS = {"zim": "kiwix", "pdf": "pdf", "epub": "epub"}


def select_lifecycle(source_id, configuration):
    """Select by format first; candidate configuration never creates trust."""
    format_name = configuration.get("format")
    adapter = FORMAT_ADAPTERS.get(format_name) if isinstance(format_name, str) else None
    if adapter == "epub" and source_id not in publication.ROUTES:
        artifact = None
        try:
            artifact = epub_source.pin(source_id, configuration, require_rights=False)
        except ValueError:
            pass
        return dict(adapter=adapter, configuration_valid=artifact is not None, artifact=artifact,
                    pdf_download=pdf_source.valid_source_url(configuration.get("source_url", "")),
                    acquisition_reason="Upload an EPUB or download from a direct HTTPS URL. Publication is separate.", actions={})
    if adapter == "pdf" and source_id not in publication.ROUTES:
        artifact = None
        try:
            artifact = pdf_source.pin(source_id, configuration, require_rights=False)
            reason = "Download from the configured HTTPS URL or upload a PDF from your computer."
        except ValueError as exc:
            reason = str(exc)
        return dict(adapter=adapter, configuration_valid=artifact is not None, artifact=artifact,
                    pdf_download=pdf_source.valid_source_url(configuration.get("source_url", "")),
                    acquisition_reason=reason, actions={})
    if adapter == "kiwix" and source_id not in publication.ROUTES:
        try:
            zim_source.candidate(configuration)
        except ValueError as exc:
            return dict(adapter=adapter, configuration_valid=False, artifact=None,
                        acquisition_reason=str(exc), actions={}, zim_download=False)
        return dict(adapter=adapter, configuration_valid=True, artifact=None,
                    acquisition_reason="", actions={}, zim_download=True)
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
        """Strict host, Basic/client or browser session, origin and CSRF gate."""
        import hmac
        config = getattr(self.server, "axiom_config", None)
        if config is None:
            host, port = self.server.server_address[:2]
            config = security.ServerConfig(host, port, frozenset({
                f"{host}:{port}", f"localhost:{port}"
            }))
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not (security.valid_public_host(hosts[0]) if config.proxied else hosts[0].lower() in config.allowed_hosts):
            self._send_json(400, {"error": "invalid host"})
            return False
        public_paths = set(PUBLIC_ICON_PATHS) | {"/", "/index.html", "/health", "/health/",
                        "/internal/publication/wikipedia-en", "/read/wikipedia-en", "/api/pdf-reader.mjs"}
        if not post and (self.path in public_paths or self.path == "/admin/login"
                         or self.path.split("?", 1)[0] in {"/", "/index.html", "/search"}
                         or pdf_source.ROUTE.fullmatch(self.path) or epub_source.ROUTE.fullmatch(self.path)
                         or ZIM_READER_ROUTE.fullmatch(self.path) or PDFJS_ASSET.fullmatch(self.path)
                         or self.path == "/internal/publication/zim"):
            return True

        # The login submission is public, but still passes all content-type,
        # size, host, fetch-site and mandatory same-origin checks below.
        login = post and self.path == "/api/admin/login"
        auth = self.headers.get_all("Authorization", [])
        local_hosts = {f"{config.host}:{config.port}", f"localhost:{config.port}"}
        if config.port == 80:
            local_hosts.update({config.host, "localhost"})
        self._session_csrf = None
        self._session_token = None
        # Browsers must use revocable sessions, not cached HTTP Basic
        # credentials that cannot be cleared by the application's Sign out.
        # Preserve Basic for explicit nonbrowser clients and deploy probes.
        browser_admin_navigation = (not post and self.path.rstrip("/") in {
            "/admin", "/admin/sources", "/admin/settings", "/admin/groupings"
        } and "text/html" in self.headers.get("Accept", "").lower())
        browser_request = (browser_admin_navigation or
                           self.headers.get("Sec-Fetch-Mode") in {
                               "navigate", "cors", "same-origin"
                           })
        basic = (not browser_request and len(auth) <= 1 and
                 not (config.store is None and hosts[0].lower() not in local_hosts)
                 and config.authorized(auth[0] if auth else None))
        if not basic and (not auth or browser_request) and config.store is not None:
            token = admin_sessions.token_from_headers(self.headers)
            csrf = ADMIN_SESSIONS.authenticate(token, config.store)
            if csrf is not None:
                self._session_csrf, self._session_token = csrf, token
        if not login and not basic and self._session_csrf is None:
            if not post and self.path.rstrip("/") in {
                    "/admin", "/admin/sources", "/admin/settings", "/admin/groupings"}:
                # Real browser navigation advertises HTML; render the branded
                # login. Legacy curl/API probes still receive their expected
                # 401 Basic challenge, preserving fixed deploy preflight checks.
                accepts = self.headers.get_all("Accept", [])
                browser_navigation = (len(accepts) == 1 and any(
                    mime.strip().split(";", 1)[0].strip().lower() == "text/html"
                    for mime in accepts[0].split(",")
                ))
                if browser_navigation:
                    self.send_response(303)
                    self.send_header("Location", "/admin/login")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            # Browser fetches never receive a native Basic challenge; explicit
            # API clients can still discover/challenge the legacy Basic realm.
            if self.headers.get("Sec-Fetch-Mode") in {"cors", "same-origin"}:
                self._send_json(401, {"error": "administrator sign in required"})
            else:
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
            if login or self._session_csrf is not None:
                if not origins:
                    self._send_json(403, {"error": "same-origin request required"})
                    return False
            if self._session_csrf is not None:
                provided = self.headers.get_all("X-Axiom-CSRF", [])
                if (len(provided) != 1 or len(provided[0]) > 256 or
                        not hmac.compare_digest(provided[0], self._session_csrf)):
                    self._send_json(403, {"error": "invalid request token"})
                    return False
            upload = self.path == "/api/pdf-upload"
            formats = self.headers.get_all("X-Document-Format", []) if upload else []
            content_type = ("application/epub+zip" if formats == ["epub"] else "application/pdf") if upload else "application/json"
            if self.headers.get_content_type() != content_type:
                self._send_json(415, {"error": content_type + " required"})
                return False
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or
                    int(lengths[0]) > (pdf_source.MAX_BYTES if upload else 16384) or self.headers.get("Transfer-Encoding")):
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
        self.send_header("Cache-Control", "no-store")
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
        if self.path == "/admin/login":
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            body = environment.get_template("admin-login.html.j2").render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in PUBLIC_ICON_PATHS:
            filename, mime = PUBLIC_ICON_PATHS[self.path]
            try:
                payload = (PUBLIC_ICON_DIR / filename).read_bytes()
            except OSError:
                self._send_json(404, {"error": "icon unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/pdf-reader.mjs":
            body = (ADMIN_TEMPLATE_DIR / "pdf-reader.mjs").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if PDFJS_ASSET.fullmatch(self.path):
            self._serve_pdfjs_asset()
            return
        epub_route = epub_source.ROUTE.fullmatch(self.path)
        if epub_route:
            self._serve_epub(*epub_route.groups())
            return
        pdf_route = pdf_source.ROUTE.fullmatch(self.path)
        if pdf_route:
            self._serve_pdf(*pdf_route.groups())
            return
        if self.path.split("?", 1)[0] in {"/", "/index.html", "/search"}:
            search_page = self.path.split("?", 1)[0] == "/search"
            sources, groups, results, selected = [], [], [], {}
            query, error, code = "", "", 200
            try:
                if len(self.path) > 4096:
                    raise ValueError("Request too long")
                params = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                                  max_num_fields=4, errors="strict")
                allowed = {"category", "collection", "tag", "q"}
                if set(params) - allowed or any(len(values) != 1 for values in params.values()):
                    raise ValueError("Invalid filters")
                query = library_search.validate_query(params.get("q", [""])[0])
                selected = {kind: params[kind][0] for kind in ("category", "collection", "tag")
                            if params.get(kind, [""])[0]}
                if any(not catalog_store.ID_RE.fullmatch(gid) for gid in selected.values()):
                    raise ValueError("Invalid filter ID")
                sources, groups = self._public_library()
                eligible = {source["source_id"] for source in sources}
                for kind, gid in selected.items():
                    plural = "categories" if kind == "category" else kind + "s"
                    group = next((g for g in groups if g["kind"] == plural and g["group_id"] == gid), None)
                    eligible.intersection_update(group["source_ids"] if group else [])
                sources = [source for source in sources if source["source_id"] in eligible]
                if search_page and query:
                    results = library_search.search(query, self._publication_snapshot, source_ids=eligible)
                    for source in sources:
                        if source["category"] in {"PDF", "EPUB"} and query.casefold() in (
                                source["name"] + " " + source["description"]).casefold():
                            results.append(dict(source_id=source["source_id"], name=source["name"],
                                                status="ok", capability=source["category"] + " title and description",
                                                results=[dict(title=source["name"], href=source["entry_path"], snippet="")]))
            except (ValueError, UnicodeError):
                error, code = "Invalid search or filter request.", 400
            except RuntimeError:
                error, code = "Search is busy. Try again.", 503
            environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
            body = environment.get_template("search.html.j2" if search_page else "library.html.j2").render(
                sources=sources, groups=results, filter_groups=groups, selected=selected,
                query=query, error=error).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif ZIM_READER_ROUTE.fullmatch(self.path):
            sid = ZIM_READER_ROUTE.fullmatch(self.path).group(1)
            try:
                records = SOURCE_CATALOG.get()["sources"]
                record = next((r for r in records if r["source_id"] == sid), None)
                state = zim_catalog.status(record, records) if record else None
                if not state or not state["published"]:
                    self._send_json(403, {"error": "ZIM collection unavailable"})
                    return
                location = publication.reader_location(zim_catalog.GENERIC_READER, zim_catalog.source_content_name(record))
                if location is None:
                    self._send_json(503, {"error": "ZIM reader entry unavailable"})
                    return
            except (OSError, ValueError, catalog_store.CatalogError):
                self._send_json(503, {"error": "ZIM publication state unavailable"})
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/internal/publication/zim":
            uri = self.headers.get("X-Axiom-Original-URI", "")
            try:
                records = SOURCE_CATALOG.get()["sources"]
                allowed = zim_catalog.authorize_reader_uri(records, uri)
            except (OSError, ValueError, catalog_store.CatalogError):
                allowed = False
            self.send_response(204 if allowed else 403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/read/wikipedia-en":
            source_id = "wikipedia-en"
            if not publication.allowed(self._publication_snapshot(), source_id):
                self._send_json(403, {"error": "collection unavailable"})
                return
            # Resolve Wikipedia by its trusted filename in the shared OPDS catalog.
            # Never choose the first book: it may be a different published ZIM.
            location = publication.reader_location(
                zim_catalog.GENERIC_READER, Path(WIKIPEDIA_SOURCE["filename"]).stem)
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
                            | {item["source_id"]: state for item, state in self._pdf_publications()}
                            | {item["source_id"]: state for item, state in self._zim_publications()})
        elif self.path.rstrip("/") == "/health":
            self._send_json(200, health_response())
        elif self.path.rstrip("/") in {"/admin", "/admin/sources", "/admin/settings", "/admin/groupings"}:
            page = {"/admin/settings": "settings", "/admin/groupings": "groupings"}.get(self.path.rstrip("/"), "sources")
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

    @staticmethod
    def _epub_record(record):
        return (record["source_id"] not in publication.ROUTES
                and record["configuration"].get("format") == "epub")

    @staticmethod
    def _zim_record(record):
        return (record["source_id"] not in publication.ROUTES
                and record["configuration"].get("format") == "zim")

    def _zim_publications(self, catalog=None):
        catalog = catalog or SOURCE_CATALOG.get()
        for record in catalog["sources"]:
            if self._zim_record(record):
                yield record, zim_catalog.status(record, catalog["sources"])

    def _public_library(self):
        """Return only published sources and groups containing those sources."""
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
            except (catalog_store.CatalogError, OSError):
                return [], []
            records = {record["source_id"]: record for record in catalog["sources"]}
            snapshot = self._publication_snapshot()
            sources = []
            for sid, route in publication.ROUTES.items():
                if sid in records and publication.status(snapshot, sid)["published"]:
                    profile = records[sid]["profile"]
                    sources.append(dict(route, source_id=sid, name=profile["title"]))
            for record, state in self._pdf_publications():
                if state["published"]:
                    profile = record["profile"]
                    sources.append(dict(source_id=record["source_id"], entry_path=state["entry_path"],
                                        symbol="E" if self._epub_record(record) else "P", category="EPUB" if self._epub_record(record) else "PDF", name=profile["title"],
                                        description=profile.get("description", ""),
                                        language=profile.get("language") or "Unspecified language"))
            for record, state in self._zim_publications(catalog):
                if state["published"]:
                    profile = record["profile"]
                    sources.append(dict(source_id=record["source_id"],
                                        entry_path=state["entry_path"], symbol="Z",
                                        category="Kiwix", name=profile["title"],
                                        description=profile.get("description", ""),
                                        language=profile.get("language") or "Unspecified language"))
            ids = {source["source_id"] for source in sources}
            groups = [dict(group, source_ids=[sid for sid in group["source_ids"] if sid in ids])
                      for group in catalog.get("groups", [])]
            groups = sorted((group for group in groups if group["source_ids"]),
                            key=lambda group: (group["name"].casefold(), group["group_id"]))
            for source in sources:
                source["memberships"] = [group["name"] for group in groups if source["source_id"] in group["source_ids"]]
            return sources, groups

    def _pdf_publications(self):
        try:
            catalog = SOURCE_CATALOG.get()
        except (catalog_store.CatalogError, OSError):
            return
        for record in catalog["sources"]:
            if self._pdf_record(record):
                artifact = select_lifecycle(record["source_id"], record["configuration"])["artifact"]
                yield record, pdf_source.status(record, artifact)
            elif self._epub_record(record):
                artifact = select_lifecycle(record["source_id"], record["configuration"])["artifact"]
                yield record, epub_source.status(record, artifact)

    def _serve_pdfjs_asset(self):
        """Serve only trusted installed renderer assets, never arbitrary host files."""
        relative = self.path[len(PDFJS_PREFIX):]
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            self._send_json(404, {"error": "asset not found"})
            return
        types = {".mjs": "text/javascript", ".js": "text/javascript",
                 ".css": "text/css", ".wasm": "application/wasm",
                 ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
                 ".ttf": "font/ttf", ".otf": "font/otf", ".icc": "application/octet-stream",
                 ".json": "application/json", ".ftl": "text/plain",
                 ".svg": "image/svg+xml", ".png": "image/png"}
        try:
            root = PDFJS_ROOT.resolve(strict=True)
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or path.suffix not in types or not path.is_file():
                raise ValueError("not an asset")
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("asset too large")
            body = path.read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"error": "PDF.js asset unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", types[path.suffix])
        self.send_header("Content-Length", str(len(body)))
        # A dedicated worker inherits its own response CSP. Permit only
        # local renderer imports and font/CMap requests, never remote fetches.
        policy = "default-src 'none'"
        if relative == "build/pdf.worker.mjs":
            policy += "; script-src 'self'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def _serve_epub(self, sid, resource, size):
        try:
            with publication.LOCK:
                if not publication.load().get(sid):
                    raise ValueError("unpublished")
                record = next((item for item in SOURCE_CATALOG.get()["sources"]
                               if item["source_id"] == sid and self._epub_record(item)), None)
                if record is None:
                    raise ValueError("unknown EPUB")
                artifact = epub_source.pin(sid, record["configuration"])
                with epub_source.installed(artifact, verify=False) as stream:
                    data = stream.read(artifact["bytes"] + 1)
                # Validate the exact immutable byte snapshot that will be served.
                epub_source._verify(BytesIO(data), artifact)
            if resource == "document.epub":
                body = data
            else:
                index = int(resource.split("/")[1]) if resource else 0
                content, labels = epub_source.chapter_content(data, index)
                environment = Environment(loader=FileSystemLoader(ADMIN_TEMPLATE_DIR), autoescape=True)
                body = environment.get_template("epub-reader.html.j2").render(
                    record=record, base=epub_source.ROUTE_PREFIX + sid, chapter=index,
                    labels=labels, content=content, size=size or "medium").encode("utf-8")
        except (OSError, ValueError):
            self._send_json(403, {"error": "document unavailable"})
            return
        self.send_response(200)
        if resource == "document.epub":
            self.send_header("Content-Type", "application/epub+zip")
            self.send_header("Content-Disposition", f'attachment; filename="{sid}.epub"')
        else:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; sandbox allow-downloads")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
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
                state = pdf_state
            elif self._epub_record(record):
                busy = False
                state = epub_source.status(record, artifacts[sid])
            verified = (bool(state and state["verified"]) if self._pdf_record(record) or self._epub_record(record)
                        else publication.verified(matched, sid))
            job = jobs.get(sid)
            if lifecycles[sid].get("zim_download"):
                try:
                    local_job = zim_jobs.status(sid)
                    if zim_jobs.matches(local_job, record["configuration"]):
                        job = local_job
                        busy = bool(local_job.get("active")) or (local_job["state"] in {"paused", "failed", "interrupted"}
                            and zim_jobs.staged_size(sid) > 0)
                        artifact = zim_jobs.installed(local_job, record["configuration"])
                        if artifact:
                            lifecycles[sid]["artifact"] = artifact
                            verified = True
                        elif local_job["state"] == "completed":
                            job = dict(local_job, state="unavailable", verified=False, detail="Installed archive unavailable")
                    elif local_job and local_job.get("active"):
                        busy = True
                    else:
                        busy = False
                except (ValueError, OSError):
                    busy = True
            if self._zim_record(record):
                state = zim_catalog.status(record, catalog["sources"])
                verified = bool(state["verified"])
            publication_filter = ("unknown" if state and state.get("error") else
                                  "published" if state and state["published"] else "unpublished")
            download_filter = "unknown"
            if verified:
                download_filter = "downloaded"
            elif not (state and state.get("error")):
                if self._pdf_record(record) or self._epub_record(record):
                    if not artifacts[sid]:
                        download_filter = "not-downloaded"
                    else:
                        # A missing file, corrupt file or unavailable mount cannot all
                        # be distinguished by the existing verification result.
                        download_filter = "unknown"
                elif lifecycles[sid].get("zim_download") and not busy:
                    download_filter = "not-downloaded"
                elif not snapshot["stale"] and not busy and (not artifacts[sid]
                        or (job and job.get("state") == "not_started")):
                    download_filter = "not-downloaded"
            sources.append(dict(record, **lifecycles[sid], acquisition=job, installed_verified=verified,
                                publication=state, mutation_blocked=busy,
                                publication_filter=publication_filter, download_filter=download_filter))
        return dict(catalog, sources=sources, acquisition_stale=snapshot["stale"])

    def _send_admin_page(self, page: str = "sources") -> None:
        """Render exactly the persisted source profiles."""
        environment = Environment(
            loader=FileSystemLoader(ADMIN_TEMPLATE_DIR),
            autoescape=True,
        )
        template = environment.get_template("admin-page.html.j2")
        try:
            catalog_snapshot = SOURCE_CATALOG.get() if page in {"sources", "groupings"} else {"revision": None, "sources": []}
        except (catalog_store.CatalogError, OSError):
            self._send_json(503, {"error": "source catalog unavailable; stored data was not replaced"})
            return
        body = template.render(
            admin_page=page,
            catalog=self._source_profiles(catalog_snapshot) if page == "sources" else catalog_snapshot if page == "groupings" else None,
            editable_fields=sorted(catalog_store.EDITABLE),
            action_labels=ACTION_LABELS,
            password_change_enabled=bool(getattr(getattr(self.server, "axiom_config", None), "store", None)),
            session_csrf_token=getattr(self, "_session_csrf", None),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server convention
        if not self._guard(post=True):
            return
        if self.path == "/api/admin/login":
            import hmac
            config = getattr(self.server, "axiom_config", None)
            if config is None or config.store is None:
                self._send_json(503, {"error": "administrator authentication unavailable"})
                return
            data = self._read_body()
            if (set(data) != {"username", "password"} or
                    not all(isinstance(data[k], str) for k in data) or
                    len(data["username"]) > 64 or len(data["password"]) > 256):
                self._send_json(400, {"error": "invalid sign-in request"})
                return
            if not (hmac.compare_digest(data["username"], config.username) and
                    config.store.verify(data["password"])):
                self._send_json(403, {"error": "invalid username or password"})
                return
            try:
                token, _ = ADMIN_SESSIONS.issue(config.store)
            except (OSError, ValueError):
                self._send_json(503, {"error": "authentication state unavailable"})
                return
            secure = config.proxied and self.headers.get("X-Forwarded-Proto") == "https"
            self.send_response(204)
            self.send_header("Set-Cookie", admin_sessions.cookie_header(token, secure=secure))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/admin/logout":
            ADMIN_SESSIONS.revoke(getattr(self, "_session_token", None))
            config = getattr(self.server, "axiom_config", None)
            secure = bool(config and config.proxied and self.headers.get("X-Forwarded-Proto") == "https")
            self.send_response(204)
            self.send_header("Set-Cookie", admin_sessions.clear_cookie_header(secure=secure))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/pdf-upload":
            self._upload_pdf()
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
            ADMIN_SESSIONS.clear()  # revoke browser sessions after credential rotation
            self._send_json(200, {"changed": True})
        elif self.path == "/api/groups":
            if not self.headers.get("Origin"):
                self._send_json(403, {"error": "same-origin request required"})
                return
            data = self._read_body()
            required = {"operation", "kind", "group_id", "revision"}
            if not required <= data.keys() or data.keys() - (required | {"name", "source_ids", "group_ids"}):
                self._send_json(400, {"error": "invalid group mutation"})
                return
            try:
                with publication.LOCK:
                    result = SOURCE_CATALOG.mutate_group(**data)
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "group catalog write failed"})
                return
            self._send_json(200, result)
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
                    catalog = SOURCE_CATALOG.get()
                    existing = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                    is_pdf = (existing and (self._pdf_record(existing) or self._epub_record(existing))) or (
                        existing is None and isinstance(sid, str) and sid not in publication.ROUTES
                        and isinstance(fields, dict) and fields.get("format") in {"pdf", "epub"})
                    busy = not is_pdf and source_busy(self._snapshot(), sid)
                    if isinstance(sid, str) and sid not in publication.ROUTES:
                        local_job = zim_jobs.status(sid)
                        if local_job and local_job.get("active"):
                            busy = True
                    unpublished = False
                    if operation == "remove":
                        # Validate all preconditions before changing publication.
                        SOURCE_CATALOG._validate_id(sid)
                        if not self._valid_revision(revision) or revision != catalog["revision"]:
                            raise catalog_store.RevisionConflict("catalog revision changed")
                        if existing is None or not isinstance(fields, dict) or fields:
                            raise catalog_store.CatalogError("remove requires an existing source and empty fields")
                        if busy:
                            raise catalog_store.CatalogError("source has active or unresolved acquisition")
                        try:
                            if publication.load().get(sid, False):
                                publication.save(sid, False)
                                unpublished = True
                        except (OSError, ValueError) as exc:
                            raise catalog_store.RevisionConflict("could not unpublish source; removal refused") from exc
                    elif isinstance(sid, str) and (sid in publication.ROUTES or is_pdf):
                        self._guard_published_mutation(operation, sid, fields, revision)
                    try:
                        result = SOURCE_CATALOG.mutate(operation, sid, fields, revision, busy)
                    except (catalog_store.CatalogError, OSError) as exc:
                        if unpublished:
                            # Never restore public access automatically after a failed removal.
                            raise catalog_store.RevisionConflict("source unpublished but profile removal failed; reload and retry removal") from exc
                        raise
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
            if (set(data) not in ({"source_id", "enabled", "revision"}, {"source_id", "enabled", "revision", "acknowledge_rights"})
                    or ("acknowledge_rights" in data and (type(data["acknowledge_rights"]) is not bool or not enabled))
                    or not isinstance(source_id, str)
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
                is_epub = record is not None and self._epub_record(record)
                is_pdf = record is not None and self._pdf_record(record)
                is_document = is_pdf or is_epub
                is_zim = record is not None and self._zim_record(record)
                if source_id not in publication.ROUTES and not (is_document or is_zim):
                    self._send_json(400, {"error": "source has no publication adapter"})
                    return
                if "acknowledge_rights" in data and not (is_document or is_zim):
                    self._send_json(400, {"error": "rights acknowledgement is for documents and ZIMs only"})
                    return
                if enabled and record is not None and (is_document or is_zim):
                    if is_zim:
                        try:
                            job = zim_jobs.status(source_id)
                            artifact = (zim_jobs.installed(job, record["configuration"])
                                        if job and job.get("verified") is True and job.get("active") is False else None)
                        except (OSError, ValueError):
                            artifact = None
                        if not artifact:
                            self._send_json(409, {"error": "a completed, verified, matching ZIM archive is required"})
                            return
                    else:
                        artifact = select_lifecycle(source_id, record["configuration"])["artifact"]
                        if not (epub_source.ready(artifact) if is_epub else pdf_source.ready(artifact, verify=True)):
                            self._send_json(409, {"error": "matching verified EPUB content is required" if is_epub else "matching verified PDF content is required"})
                            return
                    if not record["configuration"].get("license", "").strip():
                        if data.get("acknowledge_rights") is not True:
                            self._send_json(409, {"error": "Confirm you have permission to make this ZIM available to library users" if is_zim else "Confirm you have permission to make this EPUB available to library users" if is_epub else "Confirm you have permission to make this PDF available to library users"})
                            return
                        try:
                            SOURCE_CATALOG.mutate("edit", source_id, {
                                "license": "Administrator confirmed permission to make this " + ("ZIM" if is_zim else "EPUB" if is_epub else "PDF") + " available in this library; no publisher license independently verified."
                            }, data["revision"])
                        except catalog_store.RevisionConflict:
                            self._send_json(409, {"error": "catalog revision changed"})
                            return
                        except (catalog_store.CatalogError, OSError):
                            self._send_json(503, {"error": "Could not record publication acknowledgement"})
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
            if is_zim:
                # Never restart a reader from HTTP. Generate the new private
                # manifest and wait for independently monitored Kiwix OPDS.
                zim_refresh.schedule(lambda: SOURCE_CATALOG.get()["sources"])
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
            if data.get("action") in {"zim-download", "zim-resume", "zim-pause", "zim-cancel", "zim-cancel-keep", "zim-restart", "zim-revalidate"}:
                self._start_zim(data)
            elif data.get("action") == "import":
                self._import_pdf(data)
            elif data.get("action") in {"pdf-download", "epub-download", "epub-add-download"}:
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
        if (set(data) != ({"source_id", "action", "revision", "confirm_restart"} if data.get("action") == "zim-restart" else {"source_id", "action", "revision"})
                or (data.get("action") == "zim-restart" and data.get("confirm_restart") is not True)
                or not isinstance(sid, str)
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
                if not record["profile"].get("language"):
                    language = pdf_language.installed_language(artifact)
                    if language:
                        SOURCE_CATALOG.mutate("edit", sid, {"language": language}, data["revision"])
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

    def _upload_pdf(self):
        # Raw bytes; never accept a client filename or a filesystem path.
        ids = self.headers.get_all("X-Source-ID", [])
        revisions = self.headers.get_all("X-Catalog-Revision", [])
        length = int(self.headers["Content-Length"])
        additions = self.headers.get_all("X-PDF-Add", [])
        adding = additions == ["1"]
        metadata = self.headers.get_all("X-PDF-Metadata", [])
        auto_title = self.headers.get_all("X-PDF-Auto-Title", [])
        formats = self.headers.get_all("X-Document-Format", [])
        requested_format = formats[0] if formats else "pdf"
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        if (len(ids) != 1 or not catalog_store.ID_RE.fullmatch(ids[0])
                or len(revisions) != 1 or not re.fullmatch(r"[0-9]{1,20}", revisions[0])
                or length <= 0 or self.headers.get("Content-Encoding")
                or (additions and not adding) or (adding and len(metadata) != 1)
                or (not adding and metadata)
                or (auto_title and (not adding or auto_title != ["1"]))
                or len(formats) > 1 or requested_format not in {"pdf", "epub"}):
            self._send_json(400, {"error": "invalid PDF upload metadata"})
            return
        sid, revision = ids[0], int(revisions[0])
        fields = {}
        if adding:
            try:
                if len(metadata[0]) > 8192:
                    raise ValueError
                fields = json.loads(unquote(metadata[0], errors="strict"))
                SOURCE_CATALOG._validate_fields(fields, catalog_store.PROFILE_FIELDS | {"source_url", "license"},
                                                required={"title"})
                fields["format"] = requested_format
                if fields.get("source_url") and not pdf_source.valid_source_url(fields["source_url"]):
                    raise ValueError
            except (ValueError, UnicodeError):
                self._send_json(400, {"error": "invalid new PDF source metadata"})
                return
        # Read at most the verified request length before acquiring the shared lock.
        try:
            data = self.rfile.read(length)
        except OSError:
            self._send_json(400, {"error": "PDF upload interrupted"})
            return
        if len(data) != length:
            self._send_json(400, {"error": "PDF upload incomplete"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if revision != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                if adding:
                    if record is not None or sid in publication.ROUTES or len(catalog["sources"]) >= catalog_store.MAX_ENTRIES:
                        raise ValueError("source ID already exists or catalog is full")
                elif record is None or not (self._pdf_record(record) if requested_format == "pdf" else self._epub_record(record)):
                    raise ValueError("source has no matching document upload adapter")
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish source before uploading")
                config = {} if adding else record["configuration"]
                size = config.get("expected_size_bytes") or None
                digest = config.get("sha256") or None
                if size and int(size) != length:
                    raise ValueError("Upload size does not match the catalog pin")
                if requested_format == "epub":
                    observed, extracted = epub_source.install_stream(sid, BytesIO(data), expected_size=length,
                                                                    expected_sha256=digest)
                    profile = fields if adding else record["profile"]
                    for key in ("author", "publisher", "language"):
                        if not profile.get(key) and extracted.get(key):
                            fields[key] = extracted[key]
                    if adding and auto_title and extracted.get("title"):
                        fields["title"] = extracted["title"]
                else:
                    observed = pdf_source.install_stream(sid, BytesIO(data), expected_size=length,
                                                         expected_sha256=digest)
                    if adding and auto_title:
                        fields["title"] = pdf_source.simple_document_title(data) or fields["title"]
                    if not (fields if adding else record["profile"]).get("language"):
                        language = pdf_language.detect(data)
                        if language:
                            fields["language"] = language
                note = "Uploaded by administrator; SHA-256 calculated locally, not independently publisher-verified."
                provenance = config.get("provenance", "").strip()
                if provenance:
                    provenance = provenance[:catalog_store.MAX_FIELD-len(note)-1] + " " + note
                else:
                    provenance = note
                fields.update(expected_size_bytes=str(observed["bytes"]), sha256=observed["sha256"],
                              provenance=provenance)
                updated = SOURCE_CATALOG.mutate("add" if adding else "edit", sid, fields, revision)
            except catalog_store.RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except catalog_store.CatalogError:
                self._send_json(503, {"error": "source catalog unavailable"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except OSError:
                self._send_json(503, {"error": "PDF storage or catalog I/O failed"})
                return
        self._send_json(200, {"accepted": True, "installed_verified": True, "revision": updated["revision"]})

    def _download_pdf(self, data):
        sid = data.get("source_id")
        adding = data.get("action") == "epub-add-download"
        required = {"source_id", "action", "revision"} | ({"fields", "auto_title"} if adding else set())
        if (set(data) != required or not isinstance(sid, str)
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "download requires a source ID and catalog revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        fields = {}
        if adding:
            try:
                if type(data["auto_title"]) is not bool:
                    raise ValueError("invalid automatic title flag")
                fields = dict(data["fields"]) if isinstance(data["fields"], dict) else None
                SOURCE_CATALOG._validate_fields(fields, catalog_store.PROFILE_FIELDS | {"source_url", "license"},
                                                required={"title", "source_url"})
                if not pdf_source.valid_source_url(fields["source_url"]):
                    raise ValueError("A direct HTTPS EPUB URL is required")
                fields["format"] = "epub"
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid new EPUB source metadata"})
                return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((item for item in catalog["sources"] if item["source_id"] == sid), None)
                is_epub = data["action"] in {"epub-download", "epub-add-download"}
                if adding:
                    if record is not None or sid in publication.ROUTES or len(catalog["sources"]) >= catalog_store.MAX_ENTRIES:
                        raise ValueError("source ID already exists or catalog is full")
                elif record is None or not (self._epub_record(record) if is_epub else self._pdf_record(record)):
                    raise ValueError("source has no matching document download adapter")
                if publication.load().get(sid, False):
                    raise ValueError("Unpublish source before downloading")
                config = fields if adding else record["configuration"]
                url = config.get("source_url", "")
                if not pdf_source.valid_source_url(url):
                    raise ValueError("A direct HTTPS PDF URL is required")
                size = config.get("expected_size_bytes") or None
                digest = config.get("sha256") or None
                extracted = {}
                if is_epub:
                    with pdf_transport.open_pdf(url, format_name="epub") as stream:
                        observed, extracted = epub_source.install_stream(sid, stream,
                            expected_size=int(size) if size else None, expected_sha256=digest)
                else:
                    observed = pdf_transport.download(sid, url, expected_size=int(size) if size else None,
                                                      expected_sha256=digest)
                    if not record["profile"].get("language"):
                        language = pdf_language.installed_language(observed)
                        if language:
                            extracted["language"] = language
                provenance = config.get("provenance", "").strip()
                if not provenance:
                    provenance = "Downloaded via configured HTTPS URL; SHA-256 calculated locally, not independently publisher-verified."
                elif not digest:
                    suffix = " SHA-256 calculated locally, not independently publisher-verified."
                    provenance = provenance[:catalog_store.MAX_FIELD - len(suffix)] + suffix
                fields.update(expected_size_bytes=str(observed["bytes"]), sha256=observed["sha256"],
                              provenance=provenance)
                profile = fields if adding else record["profile"]
                for key in ("author", "publisher", "language"):
                    if not profile.get(key) and extracted.get(key):
                        fields[key] = extracted[key]
                if adding and data["auto_title"] and extracted.get("title"):
                    fields["title"] = extracted["title"]
                updated = SOURCE_CATALOG.mutate("add" if adding else "edit", sid, fields, data["revision"])
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

    def _start_zim(self, data):
        sid = data.get("source_id")
        if (set(data) != {"source_id", "action", "revision"} or not isinstance(sid, str)
                or data.get("action") not in {"zim-download", "zim-resume", "zim-pause", "zim-cancel", "zim-cancel-keep", "zim-restart", "zim-revalidate"}
                or not catalog_store.ID_RE.fullmatch(sid) or not self._valid_revision(data["revision"])):
            self._send_json(400, {"error": "ZIM control requires a source ID and current revision only"})
            return
        if not self.headers.get("Origin"):
            self._send_json(403, {"error": "same-origin request required"})
            return
        with publication.LOCK:
            try:
                catalog = SOURCE_CATALOG.get()
                if data["revision"] != catalog["revision"]:
                    raise ValueError("catalog revision changed")
                record = next((r for r in catalog["sources"] if r["source_id"] == sid), None)
                if not record or sid in publication.ROUTES or not select_lifecycle(sid, record["configuration"]).get("zim_download"):
                    raise ValueError("Source has no supported ZIM download URL")
                if publication.load().get(sid):
                    raise ValueError("Unpublish before downloading")
                operation = data["action"]
                if operation in {"zim-pause", "zim-cancel", "zim-cancel-keep"}:
                    result = zim_jobs.control(sid, record["configuration"], operation.removeprefix("zim-"))
                elif operation == "zim-revalidate":
                    result = zim_jobs.revalidate(sid, record["configuration"])
                elif operation == "zim-resume":
                    result = zim_jobs.start(sid, record["configuration"], resume=True)
                elif operation == "zim-restart":
                    result = zim_jobs.start(sid, record["configuration"], restart=True)
                else:
                    result = zim_jobs.start(sid, record["configuration"])
            except BlockingIOError:
                self._send_json(409, {"error": "Another ZIM acquisition is active"})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except FileNotFoundError:
                self._send_json(503, {"error": "Required storage root or staging/library parent directory is missing. Check Storage settings."})
                return
            except PermissionError:
                self._send_json(503, {"error": "Application cannot write its ZIM job records or configured storage. Check storage permissions."})
                return
            except OSError:
                self._send_json(503, {"error": "ZIM job or storage unavailable; check Storage settings before retrying"})
                return
        self._send_json(202, result)

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
                return refuse(409, life["acquisition_reason"] or "Use the ZIM Download action")
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
