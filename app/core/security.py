"""API key authentication for consumers of this service.

The service collects and stores message content, so it must not be reachable by
anything that can route to its port. Callers present a shared key in the
``X-API-Key`` header.

Configuration is a single environment variable::

    API_KEYS=key-for-app-a,key-for-app-b

* **Set** — every ``/discord/*`` endpoint requires a matching key.
* **Empty** (the default) — authentication is disabled. That keeps first-run local
  development frictionless, and the service logs a loud warning at startup and reports
  ``auth: disabled`` on ``/health`` so an unprotected deployment is never silent.

``/health`` stays open so load balancers and uptime probes work without secrets, and it
never reveals anything beyond dependency status.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings
from app.core.exceptions import DiscordServiceError
from app.core.logging import get_logger, register_secret

logger = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"

#: Declared so Swagger UI shows an "Authorize" button and sends the header.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Shared API key. Required when API_KEYS is configured.",
)


class UnauthorizedError(DiscordServiceError):
    """The caller did not present a valid API key."""

    code = "UNAUTHORIZED"
    http_status = 401


def generate_api_key() -> str:
    """Create a key suitable for handing to a consuming application."""

    return "dsk_" + secrets.token_urlsafe(32)


def register_api_key_secrets(settings: Settings) -> None:
    """Register configured keys with the log redactor so they can never be logged."""

    for key in settings.api_key_list:
        register_secret(key)


def _matches_any(candidate: str, allowed: list[str]) -> bool:
    """Constant-time membership test.

    ``compare_digest`` on every entry avoids leaking which prefix matched through
    response timing.
    """

    result = False
    for key in allowed:
        if hmac.compare_digest(candidate, key):
            result = True
    return result


async def require_api_key(request: Request) -> None:
    """FastAPI dependency enforcing the API key when one is configured."""

    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    allowed = settings.api_key_list

    if not allowed:
        return  # authentication disabled; startup already warned about it

    presented = request.headers.get(API_KEY_HEADER, "")
    if not presented:
        raise UnauthorizedError(
            f"Missing {API_KEY_HEADER} header",
            details={"header": API_KEY_HEADER},
        )
    if not _matches_any(presented, allowed):
        # Log the caller, never the key that was tried.
        logger.warning(
            "Rejected a request with an invalid API key",
            extra={
                "path": request.url.path,
                "client": request.client.host if request.client else None,
            },
        )
        raise UnauthorizedError("Invalid API key")
