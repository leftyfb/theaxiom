"""Session security regressions: opaque tokens, expiration, rotation and cookie parsing."""
import tempfile
from pathlib import Path
from email.message import Message
from unittest.mock import patch

from theaxiom.admin_security import PasswordStore
from theaxiom import admin_sessions


def _store(tmp_path, pwd="oldpass8"):
    path = tmp_path / "auth.json"
    PasswordStore.initialize(path, pwd)
    return PasswordStore(path)


def test_sessions_are_bounded_opaque_and_not_saved_with_password(tmp_path):
    store = _store(tmp_path)
    sessions = admin_sessions.Sessions()
    for index in range(admin_sessions.MAX_SESSIONS + 2):
        token, csrf = sessions.issue(store)
        assert token != csrf
        assert len(token) >= 40
        assert store.verify("oldpass8")
    assert len(sessions._entries) == admin_sessions.MAX_SESSIONS
    assert "theaxiom_session" not in (tmp_path / "auth.json").read_text()


def test_session_expires_revokes_and_password_rotation_invalidates(tmp_path):
    store = _store(tmp_path)
    sessions = admin_sessions.Sessions()
    token, csrf = sessions.issue(store)
    assert sessions.authenticate(token, store) == csrf
    sessions.revoke(token)
    assert sessions.authenticate(token, store) is None
    token, csrf = sessions.issue(store)
    assert store.change("oldpass8", "newpass8")
    assert sessions.authenticate(token, store) is None


def test_idle_and_absolute_expiry(tmp_path):
    store = _store(tmp_path)
    sessions = admin_sessions.Sessions()
    with patch.object(admin_sessions.time, "monotonic", return_value=100):
        token, _ = sessions.issue(store)
    with patch.object(admin_sessions.time, "monotonic", return_value=100 + admin_sessions.IDLE_SECONDS + 1):
        assert sessions.authenticate(token, store) is None
    with patch.object(admin_sessions.time, "monotonic", return_value=100):
        token, _ = sessions.issue(store)
    with patch.object(admin_sessions.time, "monotonic", return_value=100 + admin_sessions.ABSOLUTE_SECONDS + 1):
        assert sessions.authenticate(token, store) is None


def test_cookie_is_http_only_same_site_and_strictly_parsed():
    headers = Message()
    token = "a" * 43
    headers.add_header("Cookie", "other=x; theaxiom_session=" + token)
    assert admin_sessions.token_from_headers(headers) == token
    assert "HttpOnly" in admin_sessions.cookie_header(token)
    assert "SameSite=Strict" in admin_sessions.cookie_header(token)
    assert f"Max-Age={admin_sessions.ABSOLUTE_SECONDS}" in admin_sessions.cookie_header(token)
    assert "Secure" in admin_sessions.cookie_header(token, secure=True)
    assert "Secure" not in admin_sessions.cookie_header(token, secure=False)
    assert "Max-Age=0" in admin_sessions.clear_cookie_header()
    headers.add_header("Cookie", "theaxiom_session=" + token)
    assert admin_sessions.token_from_headers(headers) is None
    single = Message()
    single.add_header("Cookie", "theaxiom_session=" + token + "; theaxiom_session=" + token)
    assert admin_sessions.token_from_headers(single) is None
