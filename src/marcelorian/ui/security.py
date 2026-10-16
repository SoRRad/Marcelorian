"""Local UI access control (spec §2 "Local UI security", acceptance test 52).

Loopback binding is not the access control. This module provides:

* one-time bootstrap tokens. The tray opens ``/bootstrap#t=<token>``; the
  fragment never reaches the server or its logs. A static page POSTs the token
  to ``/bootstrap/exchange``, which invalidates it and sets an HttpOnly,
  SameSite=Strict session cookie. No GET request changes state.
* exact Host checks on every request, and exact Origin checks on every
  non-GET request.
* a per-session CSRF token on every state-changing request (``X-CSRF-Token``
  header, or a ``csrf_token`` field in an urlencoded form).
* security headers (CSP without inline script, no framing, no referrer,
  no caching).

Sessions are held in memory and end when the app process exits.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

from marcelorian.clock import Clock

SESSION_COOKIE = "marcelorian_session"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD"})
MAX_FORM_BYTES = 64 * 1024
PUBLIC_GET_PATHS = frozenset({"/bootstrap"})
EXCHANGE_PATH = "/bootstrap/exchange"
STATIC_PREFIX = "/static/"

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        b"connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    ),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UISession:
    session_id: str
    csrf_token: str
    created_at: datetime


class UISecurity:
    def __init__(
        self, host: str, port: int, clock: Clock, token_ttl: timedelta = timedelta(minutes=2)
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("the local UI binds to 127.0.0.1 only")
        self.host_header = f"{host}:{port}"
        self.origin = f"http://{host}:{port}"
        self._clock = clock
        self._ttl = token_ttl
        self._lock = threading.Lock()
        self._tokens: dict[str, datetime] = {}  # sha256(token) -> expiry
        self._sessions: dict[str, UISession] = {}  # sha256(session id) -> session

    # -- bootstrap ------------------------------------------------------------
    def issue_bootstrap_token(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[_digest(token)] = self._clock.now() + self._ttl
        return token

    def bootstrap_url(self, token: str) -> str:
        return f"{self.origin}/bootstrap#t={token}"

    def exchange(self, token: str) -> UISession | None:
        """Consume a bootstrap token (one time) and create a session."""
        if not token or len(token) > 256:
            return None
        key = _digest(token)
        with self._lock:
            match = next((k for k in self._tokens if hmac.compare_digest(k, key)), None)
            if match is None:
                return None
            expiry = self._tokens.pop(match)  # invalidated whether or not it expired
            if self._clock.now() >= expiry:
                return None
            session = UISession(
                session_id=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                created_at=self._clock.now(),
            )
            self._sessions[_digest(session.session_id)] = session
            return session

    def lookup(self, session_id: str | None) -> UISession | None:
        if not session_id or len(session_id) > 256:
            return None
        key = _digest(session_id)
        with self._lock:
            for stored_key, session in self._sessions.items():
                if hmac.compare_digest(stored_key, key):
                    return session
        return None

    def end_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._tokens.clear()


def _header(scope: MutableMapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


def _cookie(scope: MutableMapping[str, Any], name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


class SecurityMiddleware:
    """Pure ASGI middleware enforcing Host/Origin/session/CSRF checks."""

    def __init__(self, app: ASGIApp, security: UISecurity) -> None:
        self.app = app
        self.security = security

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await self.app(scope, receive, send)
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in existing)
                message["headers"] = headers
            await send(message)

        method = scope["method"].upper()
        path = scope["path"]

        if _header(scope, b"host") != self.security.host_header:
            await _reject(send_with_headers, 400, "Bad host")
            return
        if method not in SAFE_METHODS and _header(scope, b"origin") != self.security.origin:
            await _reject(send_with_headers, 403, "Bad origin")
            return
        if method in SAFE_METHODS and (path in PUBLIC_GET_PATHS or path.startswith(STATIC_PREFIX)):
            await self.app(scope, receive, send_with_headers)
            return
        if path == EXCHANGE_PATH and method == "POST":
            await self.app(scope, receive, send_with_headers)
            return

        session = self.security.lookup(_cookie(scope, SESSION_COOKIE))
        if session is None:
            await _reject(send_with_headers, 401, "Open Marcelorian from its tray icon to sign in.")
            return
        scope.setdefault("state", {})["ui_session"] = session

        if method in SAFE_METHODS:
            await self.app(scope, receive, send_with_headers)
            return

        supplied = _header(scope, CSRF_HEADER.encode())
        content_type = (_header(scope, b"content-type") or "").split(";")[0].strip().lower()
        if supplied is None and content_type == "application/x-www-form-urlencoded":
            body = await _read_body(receive, MAX_FORM_BYTES)
            if body is None:
                await _reject(send_with_headers, 413, "Form too large")
                return
            values = parse_qs(body.decode("utf-8", "replace")).get(CSRF_FIELD, [])
            supplied = values[0] if values else None
            receive = _replay(body)
        if supplied is None or not hmac.compare_digest(supplied, session.csrf_token):
            await _reject(send_with_headers, 403, "Missing or invalid CSRF token")
            return
        await self.app(scope, receive, send_with_headers)


async def _read_body(
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]], limit: int
) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Callable[[], Awaitable[MutableMapping[str, Any]]]:
    sent = False

    async def receive() -> MutableMapping[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _reject(
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]], status: int, text: str
) -> None:
    payload = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
