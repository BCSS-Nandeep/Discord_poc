"""Signed session cookies.

Sessions are stateless: the cookie itself carries the payload plus an HMAC-SHA256
signature, so nothing has to be stored server-side and any process can validate a
cookie another process issued.

Implemented on the standard library rather than pulling in ``itsdangerous`` or
``authlib`` -- signing and verifying a short JSON payload is about sixty lines, and the
service has no other use for those packages.

Security properties:

* **Tamper proof.** The signature covers the whole payload; any edit invalidates it.
* **Not encrypted.** The payload is readable by the browser, so it holds only a Discord
  user id and display name -- never a Discord access token.
* **Expiring.** ``exp`` is inside the signed payload, so the client cannot extend it.
* **Constant-time comparison**, to avoid leaking the signature through timing.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

SESSION_COOKIE = "discord_service_session"
OAUTH_STATE_COOKIE = "discord_service_oauth_state"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign(payload: dict[str, Any], secret: str, *, ttl_seconds: int) -> str:
    """Serialize and sign a payload, embedding its expiry."""

    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    encoded = _b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), encoded.encode(), sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify(token: str | None, secret: str) -> dict[str, Any] | None:
    """Return the payload of a valid, unexpired token, or ``None``.

    Never raises: any malformed, tampered or expired token is simply not a session.
    """

    if not token or not secret:
        return None
    try:
        encoded, provided = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(secret.encode(), encoded.encode(), sha256).digest()
    try:
        if not hmac.compare_digest(_b64decode(provided), expected):
            return None
        payload = json.loads(_b64decode(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at < int(time.time()):
        return None
    return payload


def cookie_settings(*, secure: bool, max_age: int) -> dict[str, Any]:
    """Cookie flags shared by the session and OAuth-state cookies.

    ``samesite="lax"`` still sends the cookie on the top-level redirect back from
    Discord, which a ``strict`` cookie would withhold and break the callback.
    """

    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "max_age": max_age,
        "path": "/",
    }
