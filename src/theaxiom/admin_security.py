"""Minimal LAN development server configuration and request authentication.

HTTP Basic authentication is deliberately limited to explicitly opted-in,
trusted-network development deployments. It is not encrypted transport.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    allowed_hosts: frozenset[str]
    credentials: bytes | None = None

    def authorized(self, header: str | None) -> bool:
        if self.credentials is None:
            return ipaddress.ip_address(self.host).is_loopback
        if not header or len(header) > 4096:
            return False
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            supplied = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return False
        return hmac.compare_digest(supplied, self.credentials)


def load_config(env: Mapping[str, str] | None = None) -> ServerConfig:
    env = os.environ if env is None else env
    address = ipaddress.IPv4Address(env.get("THEAXIOM_BIND_HOST", "127.0.0.1"))
    if not address.is_loopback and not any(
        address in ipaddress.IPv4Network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("bind host must be loopback or a specific private IPv4 address")
    port = int(env.get("THEAXIOM_BIND_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid bind port")
    names = {str(address)}
    if address.is_loopback:
        names.add("localhost")
    for name in env.get("THEAXIOM_ALLOWED_HOSTS", "").split(","):
        if not name:
            continue
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name):
            raise ValueError("invalid allowed host name")
        names.add(name)
    hosts = frozenset(f"{name}:{port}" if port != 80 else name for name in names)
    if port == 80:
        hosts |= frozenset(f"{name}:80" for name in names)
    username = env.get("THEAXIOM_ADMIN_USER", "")
    password_file = env.get("THEAXIOM_ADMIN_PASSWORD_FILE", "")
    credentials = None
    if username or password_file:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username) or not password_file:
            raise ValueError("admin username and password file are both required")
        path = Path(password_file)
        # Open the credential file once, rejecting symlinks, devices and
        # group/other access. Do not include its contents in diagnostics.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("admin password file must be a private regular file")
            password = stream.read(1026).rstrip(b"\r\n")
        if not 16 <= len(password) <= 1024 or any(c < 33 or c > 126 for c in password):
            raise ValueError("admin password must contain 16–1024 printable non-space ASCII characters")
        credentials = username.encode("ascii") + b":" + password
    if not address.is_loopback and (credentials is None or env.get("THEAXIOM_ALLOW_INSECURE_LAN_HTTP") != "1"):
        raise ValueError("LAN HTTP requires authentication and explicit insecure transport opt-in")
    return ServerConfig(str(address), port, hosts, credentials)
