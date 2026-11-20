"""Trusted-LAN configuration and HTTP authentication regressions."""
import base64
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from theaxiom import admin_api, catalog_store
from theaxiom.admin_security import PasswordStore, ServerConfig, load_config


class ConfigTests(unittest.TestCase):
    def test_default_is_loopback(self):
        config = load_config({})
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8080))

    def test_nginx_requires_hashed_credentials_and_public_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            PasswordStore.initialize(path, "fixture-password-only")
            env = {"THEAXIOM_BEHIND_NGINX": "1", "THEAXIOM_PUBLIC_HOSTS": "vancouver,192.168.0.4"}
            with self.assertRaises(ValueError):
                load_config(env)
            env.update(THEAXIOM_ADMIN_USER="admin", THEAXIOM_ADMIN_CREDENTIALS_FILE=str(path))
            config = load_config(env)
            self.assertIn("vancouver", config.allowed_hosts)
            self.assertFalse(config.authorized(None))
            self.assertTrue(config.authorized("Basic " + base64.b64encode(b"admin:fixture-password-only").decode()))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(env)

    def test_initialization_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            PasswordStore.initialize(path, "fixture-password-only")
            with self.assertRaises(FileExistsError):
                PasswordStore.initialize(path, "different-password")
            self.assertTrue(PasswordStore(path).verify("fixture-password-only"))
            self.assertNotIn("fixture-password-only", path.read_text())

    def test_unsafe_binding_rejected(self):
        for host in ("0.0.0.0", "8.8.8.8", "192.168.0.4", "::", "example.org"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                load_config({"THEAXIOM_BIND_HOST": host})


class ReleaseRevisionTests(unittest.TestCase):
    def test_release_revision_accepts_only_full_lowercase_sha(self):
        for value, expected in [("a" * 40 + "\n", "a" * 40),
                                ("A" * 40, None), ("a" * 39, None),
                                ("not-a-revision", None)]:
            with self.subTest(value=value), patch.object(
                admin_api.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, value)
            ) as run:
                self.assertEqual(admin_api._release_revision(), expected)
                self.assertEqual(run.call_args.kwargs["timeout"], 2)
                self.assertTrue(run.call_args.kwargs["check"])

    def test_release_revision_lookup_failure_is_nonfatal(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 2),
                      subprocess.CalledProcessError(1, "git")):
            with self.subTest(error=error), patch.object(admin_api.subprocess, "run", side_effect=error):
                self.assertIsNone(admin_api._release_revision())

    def test_health_uses_cached_revision_without_invoking_git(self):
        with patch.object(admin_api, "RELEASE_REVISION", "b" * 40), patch.object(
            admin_api.subprocess, "run", side_effect=AssertionError("unexpected Git invocation")
        ):
            self.assertEqual(admin_api.health_response(), {
                "status": "ok", "service": "offline-library-admin-api", "revision": "b" * 40
            })


class HTTPAuthTests(unittest.TestCase):
    def setUp(self):
        self.server = admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(admin_api, "SOURCE_CATALOG",
                                       catalog_store.CatalogStore(Path(directory.name) / "sources.json")))
        self.credential_path = Path(directory.name) / "credential"
        PasswordStore.initialize(self.credential_path, "fixture-password-only")
        self.server.axiom_config = ServerConfig("127.0.0.1", self.port, frozenset({self.host}),
                                                "admin", PasswordStore(self.credential_path))
        self.auth = "Basic " + base64.b64encode(b"admin:fixture-password-only").decode()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method="GET", auth=None, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        fields = dict(headers or {})
        if auth is not None:
            fields["Authorization"] = auth
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health_public_but_admin_routes_protected(self):
        with patch.object(admin_api, "RELEASE_REVISION", "c" * 40):
            status, headers, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual(json.loads(body), {
            "status": "ok", "service": "offline-library-admin-api", "revision": "c" * 40
        })
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertIn(b"Your offline reference library", body)
        for path in ("/admin", "/admin/", "/admin/sources", "/admin/settings", "/admin/sources/", "/admin/settings/", "/admin/groupings", "/admin/groupings/", "/api/storage-settings",
                     "/api/storage-status", "/api/acquisitions", "/api/freshness", "/api/jobs/" + "a"*32):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)
        for path in ("/api/storage-settings", "/api/action", "/api/admin/password"):
            self.assertEqual(self.request(path, "POST", body="{}")[0], 401)
        self.assertEqual(self.request("/api/action", "POST", body='{"action":"import"}')[0], 401)
        self.assertEqual(self.request("/api/library/pdf/fixture-guide/document.pdf")[0], 403)

    def test_brand_icons_load_without_admin_credentials(self):
        """Browser favicon requests must not trigger an admin password prompt."""
        icons = {
            "/favicon.svg": ("image/svg+xml", b"<svg"),
            "/favicon.png": ("image/png", b"\\x89PNG\\r\\n\\x1a\\n"),
            "/favicon.ico": ("image/x-icon", b"\\x00\\x00\\x01\\x00"),
        }
        for path, (mime, magic) in icons.items():
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 200)
                self.assertEqual(headers["Content-Type"], mime)
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertIn("nosniff", headers["X-Content-Type-Options"])
                self.assertTrue(body.startswith(magic))
                self.assertNotIn("WWW-Authenticate", headers)
                self.assertEqual(self.request(path, headers={"Host": "attacker.invalid"})[0], 400)
        status, headers, _ = self.request("/admin")
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], 'Basic realm="The Axiom", charset="UTF-8"')

    def test_invalid_credentials_and_host_rejected(self):
        self.assertEqual(self.request("/admin", auth="Basic bad")[0], 401)
        self.assertEqual(self.request("/admin", auth=self.auth, headers={"Host":"attacker.invalid"})[0], 400)
        self.assertEqual(self.request("/admin", auth=self.auth)[0], 200)
        status, headers, _ = self.request("/", auth=self.auth)
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)

    def test_public_search_and_validation(self):
        code, headers, body = self.request("/search?q=water")
        self.assertEqual(code, 200)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertIn(b"Results for", body)
        self.assertIn(b"No published collections", body)
        for path in ("/search?q=a&q=b", "/search?q=%00", "/search?other=x",
                     "/search?q=" + "x" * 161):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/search?q=x", headers={"Host":"attacker.invalid"})[0], 400)
        self.assertIn(b'action="/search"', self.request("/")[2])

    def test_admin_pages_are_separate(self):
        for path in ("/admin", "/admin/sources", "/admin/sources/"):
            code, _, body = self.request(path, auth=self.auth)
            self.assertEqual(code, 200)
            self.assertIn(b'id="source-cards"', body)
            self.assertNotIn(b'id="storage-form"', body)
        for path in ("/admin/settings", "/admin/settings/"):
            code, _, body = self.request(path, auth=self.auth)
            self.assertEqual(code, 200)
            self.assertIn(b'id="storage-form"', body)
            self.assertIn(b'id="password-form"', body)
            self.assertNotIn(b'id="acquisition-monitor-script"', body)
            self.assertNotIn(b'id="source-cards"', body)

    def test_cross_origin_and_simple_posts_rejected(self):
        for headers, expected in [
            ({"Origin":"http://attacker.invalid", "Content-Type":"application/json"}, 403),
            ({"Sec-Fetch-Site":"cross-site", "Content-Type":"application/json"}, 403),
            ({"Content-Type":"text/plain"}, 415),
        ]:
            with patch.object(admin_api.storage_settings, "save_settings") as save:
                self.assertEqual(self.request("/api/storage-settings","POST",self.auth,headers,"{}")[0],expected)
                save.assert_not_called()

    def test_password_change_verifies_current_password_and_persists(self):
        headers = {"Content-Type":"application/json", "Origin":"http://"+self.host}
        new = "eight123"
        payload = {"current_password":"incorrect-password",
                   "new_password":new, "confirm_password":new}
        self.assertEqual(self.request("/api/admin/password", "POST", self.auth,
                                     headers, json.dumps(payload))[0], 403)
        payload["current_password"] = "fixture-password-only"
        self.assertEqual(self.request("/api/admin/password", "POST", self.auth,
                    {"Content-Type":"application/json"}, json.dumps(payload))[0], 403)
        payload["confirm_password"] = "mismatch"
        self.assertEqual(self.request("/api/admin/password", "POST", self.auth,
                                     headers, json.dumps(payload))[0], 400)
        payload["confirm_password"] = new
        self.assertEqual(self.request("/api/admin/password", "POST", self.auth,
                                     headers, json.dumps(payload))[0], 200)
        self.assertEqual(self.request("/admin", auth=self.auth)[0], 401)
        new_auth = "Basic " + base64.b64encode(("admin:"+new).encode()).decode()
        self.assertEqual(self.request("/admin", auth=new_auth)[0], 200)
        self.assertTrue(PasswordStore(self.credential_path).verify(new))
        self.assertFalse(PasswordStore(self.credential_path).verify("fixture-password-only"))
        self.assertEqual(self.credential_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(new, self.credential_path.read_text())

    def test_authenticated_acquisition_remains_disabled(self):
        with patch.object(admin_api.subprocess, "Popen") as launch, patch.object(
            self.server, "acquisition_monitor", create=True,
        ) as monitor:
            monitor.snapshot.return_value = {"stale": False, "acquisitions": []}
            code, _, _ = self.request("/api/action", "POST", self.auth,
                {"Content-Type":"application/json","Origin":"http://"+self.host},
                '{"source_id":"wikipedia-en","action":"download","revision":0}')
            self.assertEqual(code, 503)
            launch.assert_not_called()


class PasswordLengthTests(unittest.TestCase):
    def test_initialization_and_rotation_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            with self.assertRaises(ValueError):
                PasswordStore.initialize(path, "seven12")
            self.assertFalse(path.exists())
            PasswordStore.initialize(path, "eight123")
            store = PasswordStore(path)
            self.assertTrue(store.verify("eight123"))
            with self.assertRaises(ValueError):
                store.change("eight123", "seven12")
            self.assertTrue(store.verify("eight123"))
            self.assertTrue(store.change("eight123", "newpass8"))
            self.assertTrue(store.verify("newpass8"))
            self.assertFalse(store.verify("eight123"))


class AdminProxyRouteTests(unittest.TestCase):
    def test_all_admin_navigation_routes_reach_authenticated_backend(self):
        config = (Path(__file__).parents[1] / "deploy/nginx/theaxiom.conf").read_text()
        for route in ("/admin/sources", "/admin/groupings", "/admin/settings"):
            for path in (route, route + "/"):
                with self.subTest(path=path):
                    self.assertIn("location = " + path + " { include /etc/nginx/snippets/theaxiom-backend.conf; }", config)


class BrandingAssetTests(unittest.TestCase):
    def test_all_shipped_html_templates_use_local_brand_identity(self):
        root = Path(__file__).resolve().parents[1]
        pages = [
            "templates/admin-page.html.j2", "templates/library.html.j2",
            "templates/search.html.j2", "templates/document-page.html.j2",
            "templates/epub-reader.html.j2", "templates/pdf-reader.html.j2",
            "deploy/www/index.html",
        ]
        for page in pages:
            with self.subTest(page=page):
                html = (root / page).read_text()
                self.assertIn('<meta name="application-name" content="The Axiom">', html)
                self.assertIn('href="/favicon.svg"', html)
                self.assertIn('href="/favicon.png"', html)
                self.assertIn('href="/favicon.ico"', html)
                self.assertIn("<title>", html)
                self.assertIn("The Axiom</title>", html)

    def test_nginx_serves_icons_without_auth_or_proxy_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        conf = (root / "deploy/nginx/theaxiom.conf").read_text()
        for icon in ("favicon.svg", "favicon.png", "favicon.ico"):
            block = conf.split("location = /" + icon + " {", 1)[1].split("}", 1)[0]
            self.assertIn("try_files $uri =404;", block)
            self.assertNotIn("auth_request", block)
            self.assertNotIn("proxy_pass", block)
        icons = root / "deploy/www"
        self.assertTrue((icons / "favicon.svg").read_bytes().startswith(b"<svg"))
        self.assertTrue((icons / "favicon.png").read_bytes().startswith(b"\\x89PNG\\r\\n\\x1a\\n"))
        self.assertTrue((icons / "favicon.ico").read_bytes().startswith(b"\\x00\\x00\\x01\\x00"))
