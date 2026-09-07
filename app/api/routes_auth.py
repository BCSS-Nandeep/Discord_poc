"""Discord OAuth2 login routes.

These endpoints authenticate a **person** to this service. They are unauthenticated by
necessity -- they are how a session is obtained in the first place.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import ServicesDep, SettingsDep
from app.core.logging import get_logger
from app.core.sessions import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    cookie_settings,
    sign,
    verify,
)
from app.discord.oauth_service import OAuthFailedError
from app.schemas.auth import AuthStatusResponse, SessionUser

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: The state cookie only has to survive the round trip to Discord.
STATE_TTL_SECONDS = 600


@router.get(
    "/discord/login",
    summary="Start Discord login",
    description=(
        "Redirects the browser to Discord's consent screen. A random `state` is stored "
        "in a short-lived signed cookie and checked on the callback, which is what "
        "prevents login CSRF."
    ),
)
async def discord_login(settings: SettingsDep, services: ServicesDep) -> RedirectResponse:
    """Send the user to Discord to authorize."""

    redirect = services.oauth.build_login_redirect()
    response = RedirectResponse(url=redirect.url, status_code=307)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        sign({"state": redirect.state}, settings.session_signing_key,
             ttl_seconds=STATE_TTL_SECONDS),
        **cookie_settings(
            secure=settings.session_cookie_secure, max_age=STATE_TTL_SECONDS
        ),
    )
    return response


@router.get(
    "/discord/callback",
    summary="Discord login callback",
    description=(
        "Where Discord returns the user. Verifies `state`, exchanges the authorization "
        "code for an access token, identifies the user, then discards the token and "
        "issues a signed session cookie."
    ),
)
async def discord_callback(
    request: Request,
    settings: SettingsDep,
    services: ServicesDep,
    code: Annotated[str | None, Query(description="Authorization code from Discord.")] = None,
    state: Annotated[str | None, Query(description="Opaque CSRF state.")] = None,
    error: Annotated[str | None, Query(description="Set when the user declined.")] = None,
) -> RedirectResponse:
    """Complete the login and start a session."""

    if error:
        logger.info("User declined the Discord login", extra={"error": error})
        return RedirectResponse(url="/ui?login=denied", status_code=307)

    stored = verify(request.cookies.get(OAUTH_STATE_COOKIE), settings.session_signing_key)
    services.oauth.verify_state(state, (stored or {}).get("state"))

    if not code:
        raise OAuthFailedError(
            "Discord did not return an authorization code.",
            details={"reason": "MISSING_CODE"},
        )

    user = await services.oauth.complete_login(code)
    # Commit before redirecting so the recorded login survives even if the browser
    # follows the redirect faster than the session teardown would otherwise flush.
    await services.session.commit()

    response = RedirectResponse(url="/ui?login=ok", status_code=307)
    response.set_cookie(
        SESSION_COOKIE,
        sign(
            services.oauth.session_payload(user),
            settings.session_signing_key,
            ttl_seconds=settings.session_ttl_hours * 3600,
        ),
        **cookie_settings(
            secure=settings.session_cookie_secure,
            max_age=settings.session_ttl_hours * 3600,
        ),
    )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@router.get(
    "/me",
    response_model=AuthStatusResponse,
    summary="Who am I",
    description=(
        "Reports the current session. Always 200 so a client can branch on "
        "`authenticated` rather than handling an error."
    ),
)
async def whoami(request: Request, settings: SettingsDep) -> AuthStatusResponse:
    """Return the signed-in user, if any."""

    payload = verify(request.cookies.get(SESSION_COOKIE), settings.session_signing_key)
    return AuthStatusResponse(
        authenticated=payload is not None,
        oauth_enabled=settings.oauth_enabled,
        api_key_required=settings.auth_enabled,
        user=(
            SessionUser(
                discord_user_id=payload["sub"],
                username=payload.get("username"),
                global_name=payload.get("global_name"),
                avatar_url=payload.get("avatar_url"),
            )
            if payload
            else None
        ),
    )


@router.post(
    "/logout",
    summary="Log out",
    description="Clears the session cookie. Always succeeds.",
)
async def logout(settings: SettingsDep) -> Response:
    """End the session."""

    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response
