"""Trusted-LAN configuration and HTTP authentication regressions."""
import base64
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from theaxiom import admin_api
from theaxiom.admin_security import ServerConfig, load_config


class ConfigTests(unittest.TestCase):
    def test_default_is_loopback(self):
        config = load_config({})
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8080))

    def test_lan_requires_explicit_auth_and_transport_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("fixture-password-only\n")
            path.chmod(0o600)
            env = {"THEAXIOM_BIND_HOST": "192.168.0.4", "THEAXIOM_BIND_PORT": "80",
                   "THEAXIOM_ALLOWED_HOSTS": "vancouver",
                   "THEAXIOM_ADMIN_USER": "admin", "THEAXIOM_ADMIN_PASSWORD_FILE": str(path)}
            with self.assertRaises(ValueError):
                load_config(env)
            env["THEAXIOM_ALLOW_INSECURE_LAN_HTTP"] = "1"
            config = load_config(env)
            self.assertIn("vancouver", config.allowed_hosts)
            self.assertFalse(config.authorized(None))
            self.assertTrue(config.authorized("Basic " + base64.b64encode(b"admin:fixture-password-only").decode()))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(env)

    def test_unsafe_binding_rejected(self):
        for host in ("0.0.0.0", "8.8.8.8", "::", "example.org"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                load_config({"THEAXIOM_BIND_HOST": host})


class HTTPAuthTests(unittest.TestCase):
    def setUp(self):
        self.server = admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.server.axiom_config = ServerConfig("127.0.0.1", self.port, frozenset({self.host}),
                                                b"admin:fixture-password-only")
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
        self.assertEqual(self.request("/health")[0], 200)
        for path in ("/", "/admin", "/admin/", "/api/storage-settings",
                     "/api/storage-status", "/api/freshness", "/api/jobs/" + "a"*32):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)
        for path in ("/api/storage-settings", "/api/action"):
            self.assertEqual(self.request(path, "POST", body="{}")[0], 401)

    def test_invalid_credentials_and_host_rejected(self):
        self.assertEqual(self.request("/admin", auth="Basic bad")[0], 401)
        self.assertEqual(self.request("/admin", auth=self.auth, headers={"Host":"attacker.invalid"})[0], 400)
        self.assertEqual(self.request("/admin", auth=self.auth)[0], 200)
        status, headers, _ = self.request("/", auth=self.auth)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/admin")

    def test_cross_origin_and_simple_posts_rejected(self):
        for headers, expected in [
            ({"Origin":"http://attacker.invalid", "Content-Type":"application/json"}, 403),
            ({"Sec-Fetch-Site":"cross-site", "Content-Type":"application/json"}, 403),
            ({"Content-Type":"text/plain"}, 415),
        ]:
            with patch.object(admin_api.storage_settings, "save_settings") as save:
                self.assertEqual(self.request("/api/storage-settings","POST",self.auth,headers,"{}")[0],expected)
                save.assert_not_called()

    def test_authenticated_acquisition_remains_disabled(self):
        with patch.object(admin_api.subprocess, "Popen") as launch:
            code, _, _ = self.request("/api/action", "POST", self.auth,
                {"Content-Type":"application/json","Origin":"http://"+self.host},
                '{"source_id":"wikipedia-en","action":"download"}')
            self.assertEqual(code, 503)
            launch.assert_not_called()
