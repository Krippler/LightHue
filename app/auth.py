"""Optional console password.

The console is open by default, which is the right shape for a LAN toy. Set a
password from the Settings panel and every /api route plus the websocket start
requiring it — no container config involved either way.

Passwords are stored as a PBKDF2 record, never in the clear. Browsers get an
opaque session cookie; scripts can send the password in an X-Console-Password
header instead.
"""

import hashlib
import hmac
import os
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

from . import config_store

COOKIE_NAME = "hue_console_session"
_ITERATIONS = 200_000

# Sessions live in memory only: a restart signs everyone out, which is a fine
# trade for not persisting bearer credentials to disk.
_sessions: set[str] = set()

# Reachable before you are authenticated: the UI shell itself, and the endpoint
# that tells it whether a password is even required.
_PUBLIC_PATHS = {"/", "/api/auth", "/api/auth/login"}


def hash_password(password: str) -> dict:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return {
        "algo": "pbkdf2_sha256",
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(record: dict, password: str) -> bool:
    if not record:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(record["salt"]),
            int(record["iterations"]),
        )
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), record.get("hash", ""))


def password_record() -> dict | None:
    return config_store.load().get("auth")


def is_enabled() -> bool:
    return password_record() is not None


def set_password(password: str):
    config_store.update(auth=hash_password(password))
    _sessions.clear()   # a password change invalidates everyone


def clear_password():
    config_store.update(auth=None)
    _sessions.clear()


def open_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    return token


def close_session(token: str | None):
    if token:
        _sessions.discard(token)


def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    # compare_digest against each known session so a wrong guess doesn't leak
    # its length or prefix through timing.
    return any(hmac.compare_digest(token, known) for known in list(_sessions))


def _headers(scope: Scope) -> dict:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookie(headers: dict, name: str) -> str | None:
    raw = headers.get("cookie", "")
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def is_authorized(scope: Scope) -> bool:
    record = password_record()
    if record is None:
        return True
    headers = _headers(scope)
    if _session_valid(_cookie(headers, COOKIE_NAME)):
        return True
    supplied = headers.get("x-console-password")
    if supplied is None:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:]
    return bool(supplied) and verify_password(record, supplied)


class ConsoleAuthMiddleware:
    """Gates /api and /ws when a password is set. Static assets stay public so
    the UI can load far enough to show a login prompt."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        gated = path == "/ws" or path.startswith("/api")
        if not gated or path in _PUBLIC_PATHS or is_authorized(scope):
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail":"Console password required"}',
        })
