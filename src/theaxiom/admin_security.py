"""Application-owned admin authentication behind the standard Nginx frontend."""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ITERATIONS = 600_000


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError("password must contain 8–256 characters")
    if any(ord(c) < 33 or ord(c) > 126 for c in password):
        raise ValueError("password must use printable non-space ASCII characters")
    return password.encode("ascii")


def _record(password: str) -> dict:
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", _password_bytes(password), salt, ITERATIONS)
    return {"version": 1, "algorithm": "pbkdf2-sha256", "iterations": ITERATIONS,
            "salt": salt.hex(), "digest": digest.hex()}


class PasswordStore:
    """Private, atomically replaced credential file; single backend process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._load()  # fail closed before the server starts

    def _load(self) -> dict:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("credential file must be a private regular file")
            if metadata.st_size > 4096:
                raise ValueError("invalid credential file")
            value = json.loads(stream.read(4097))
        if (not isinstance(value, dict) or set(value) !=
                {"version", "algorithm", "iterations", "salt", "digest"} or
                type(value["version"]) is not int or value["version"] != 1 or
                value["algorithm"] != "pbkdf2-sha256" or value["iterations"] != ITERATIONS or
                any(not isinstance(value[key], str) or
                    not re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in ("salt", "digest"))):
            raise ValueError("invalid credential file")
        return value

    @staticmethod
    def _matches(password: str, record: dict) -> bool:
        try:
            candidate = _password_bytes(password)
        except ValueError:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", candidate, bytes.fromhex(record["salt"]), ITERATIONS)
        return hmac.compare_digest(digest, bytes.fromhex(record["digest"]))

    def verify(self, password: str) -> bool:
        with self._lock:
            try:
                return self._matches(password, self._load())
            except (OSError, ValueError):
                return False

    def change(self, current: str, new: str) -> bool:
        with self._lock:
            if not self._matches(current, self._load()):
                return False
            if current == new:
                raise ValueError("choose a different password")
            self._write(self.path, _record(new), replace=True)
            return True

    @staticmethod
    def _write(path: Path, record: dict, *, replace: bool) -> None:
        # Parent directory must already exist and be controlled by the service
        # account. Never write credentials into the content mount or web root.
        fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)  # atomic initialization without overwriting
        finally:
            Path(temporary).unlink(missing_ok=True)

    @classmethod
    def initialize(cls, path: Path, password: str) -> None:
        cls._write(Path(path), _record(password), replace=False)


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    allowed_hosts: frozenset[str]
    username: str = ""
    store: PasswordStore | None = None
    proxied: bool = False

    def authorized(self, header: str | None) -> bool:
        if self.store is None:
            return not self.proxied and ipaddress.ip_address(self.host).is_loopback
        if not header or len(header) > 4096:
            return False
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            supplied = base64.b64decode(encoded, validate=True).decode("ascii")
        except (ValueError, binascii.Error, UnicodeError):
            return False
        user, separator, password = supplied.partition(":")
        return bool(separator and hmac.compare_digest(user, self.username)
                    and self.store.verify(password))


def valid_public_host(value: str) -> bool:
    """Accept syntactically valid public hostnames/IPs, never arbitrary header data."""
    if not isinstance(value, str) or len(value) > 255 or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        return False
    if re.fullmatch(r"\\[[0-9a-fA-F:.]+\\](?::[0-9]{1,5})?", value):
        try:
            ipaddress.IPv6Address(value[1:value.index("]")])
            return True
        except ValueError:
            return False
    return bool(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,252}[a-zA-Z0-9])?(?::[0-9]{1,5})?", value))


def load_config(env: Mapping[str, str] | None = None) -> ServerConfig:
    env = os.environ if env is None else env
    address = ipaddress.IPv4Address(env.get("THEAXIOM_BIND_HOST", "127.0.0.1"))
    if not address.is_loopback:
        raise ValueError("the backend must bind to loopback; use Nginx for LAN access")
    port = int(env.get("THEAXIOM_BIND_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid bind port")
    proxied = env.get("THEAXIOM_BEHIND_NGINX", "0") == "1"
    hosts = {f"{address}:{port}", f"localhost:{port}"}
    if port == 80:
        hosts.update({str(address), "localhost"})
    if proxied:
        for item in env.get("THEAXIOM_PUBLIC_HOSTS", "").split(","):
            item = item.strip().lower()
            if not item:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}(?::[0-9]{1,5})?", item):
                raise ValueError("invalid public host")
            hosts.add(item)
    username = env.get("THEAXIOM_ADMIN_USER", "")
    credential_file = env.get("THEAXIOM_ADMIN_CREDENTIALS_FILE", "")
    store = None
    if env.get("THEAXIOM_ADMIN_PASSWORD_FILE"):
        raise ValueError("plaintext password files are unsupported; initialize a hashed credential file")
    if username or credential_file:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username) or not credential_file:
            raise ValueError("admin username and credential file are both required")
        store = PasswordStore(Path(credential_file))
    if proxied and store is None:
        raise ValueError("Nginx mode requires admin authentication")
    return ServerConfig(str(address), port, frozenset(hosts), username, store, proxied)


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the private Axiom admin credential file")
    parser.add_argument("command", choices=("init",))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    password = getpass.getpass("Initial admin password: ")
    if password != getpass.getpass("Confirm admin password: "):
        parser.error("passwords do not match")
    try:
        PasswordStore.initialize(args.path, password)
    except FileExistsError:
        parser.error("credential file already exists; use the admin password change interface")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
