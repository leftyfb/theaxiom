"""Trusted-LAN configuration and HTTP authentication regressions."""
import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from theaxiom import admin_api
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


class HTTPAuthTests(unittest.TestCase):
    def setUp(self):
        self.server = admin_api.ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminAPIHandler)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
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
        self.assertEqual(self.request("/health")[0], 200)
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertIn(b"Your offline reference library", body)
        for path in ("/admin", "/admin/", "/api/storage-settings",
                     "/api/storage-status", "/api/acquisitions", "/api/freshness", "/api/jobs/" + "a"*32):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)
        for path in ("/api/storage-settings", "/api/action", "/api/admin/password"):
            self.assertEqual(self.request(path, "POST", body="{}")[0], 401)

    def test_invalid_credentials_and_host_rejected(self):
        self.assertEqual(self.request("/admin", auth="Basic bad")[0], 401)
        self.assertEqual(self.request("/admin", auth=self.auth, headers={"Host":"attacker.invalid"})[0], 400)
        self.assertEqual(self.request("/admin", auth=self.auth)[0], 200)
        status, headers, _ = self.request("/", auth=self.auth)
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)

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
        new = "replacement-fixture-password"
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
        with patch.object(admin_api.subprocess, "Popen") as launch:
            code, _, _ = self.request("/api/action", "POST", self.auth,
                {"Content-Type":"application/json","Origin":"http://"+self.host},
                '{"source_id":"wikipedia-en","action":"download"}')
            self.assertEqual(code, 503)
            launch.assert_not_called()
