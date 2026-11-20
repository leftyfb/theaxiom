"""Opaque bounded in-memory administrator sessions, separate from Basic API credentials.

Only random bearer tokens are placed in browser cookies. The password-derived
credential record stays in the existing private PasswordStore; no passwords,
tokens or CSRF secrets are stored on disk. Restart invalidates sessions.
"""
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import threading
import time

COOKIE_NAME = "theaxiom_session"
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 64
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,128}\Z")


@dataclass
class _Entry:
    csrf: str
    credential_digest: str
    created_at: float
    last_seen: float


class Sessions:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}

    @staticmethod
    def _credential_digest(store):
        # A password change generates a different PBKDF2 digest. Checking this
        # private record does not re-hash the plaintext or expose credentials.
        with store._lock:
            return store._load()["digest"]

    def _prune(self, now):
        for key, entry in tuple(self._entries.items()):
            if now - entry.last_seen > IDLE_SECONDS or now - entry.created_at > ABSOLUTE_SECONDS:
                self._entries.pop(key, None)

    @staticmethod
    def _key(token):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return None
        return hashlib.sha256(token.encode("ascii")).digest()

    def issue(self, store):
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        key = self._key(token)
        entry = _Entry(secrets.token_urlsafe(32), self._credential_digest(store), now, now)
        with self._lock:
            self._prune(now)
            if len(self._entries) >= MAX_SESSIONS:
                oldest = min(self._entries, key=lambda k: self._entries[k].created_at)
                self._entries.pop(oldest)
            self._entries[key] = entry
        return token, entry.csrf

    def authenticate(self, token, store):
        key = self._key(token)
        if key is None or store is None:
            return None
        now = time.monotonic()
        try:
            credential_digest = self._credential_digest(store)
        except (OSError, ValueError):
            return None
        with self._lock:
            self._prune(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            if not hmac.compare_digest(entry.credential_digest, credential_digest):
                self._entries.pop(key, None)
                return None
            entry.last_seen = now
            return entry.csrf

    def revoke(self, token):
        key = self._key(token)
        if key is not None:
            with self._lock:
                self._entries.pop(key, None)

    def clear(self):
        with self._lock:
            self._entries.clear()


def cookie_header(token, *, secure=False):
    """A single root-scoped HttpOnly session cookie. Never log this header."""
    suffix = "; Secure" if secure else ""
    return (f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={IDLE_SECONDS}{suffix}")


def clear_cookie_header(*, secure=False):
    return cookie_header("", secure=secure).replace(f"Max-Age={IDLE_SECONDS}", "Max-Age=0")


def token_from_headers(headers):
    """Strictly parse only the one expected browser session cookie."""
    values = headers.get_all("Cookie", [])
    if len(values) != 1 or len(values[0]) > 4096:
        return None
    seen = []
    for part in values[0].split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == COOKIE_NAME:
            seen.append(value)
    return seen[0] if len(seen) == 1 and TOKEN_PATTERN.fullmatch(seen[0]) else None
