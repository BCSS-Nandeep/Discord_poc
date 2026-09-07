"""Discord OAuth2 authorization-code login.

This logs a **person** in to this service. It is entirely separate from message
collection, which uses the bot token: no OAuth2 scope grants a third-party app access
to message history, so this adds identity, not reach.

The flow, and where each guarantee comes from:

1. ``/auth/discord/login`` mints a random ``state``, stores it in a short-lived signed
   cookie, and redirects to Discord.
2. The user authorizes on Discord.
3. ``/auth/discord/callback`` checks the returned ``state`` against that cookie. This
   is the CSRF defence: without it an attacker could feed a victim a callback URL
   carrying their own authorization code.
4. The code is exchanged for an access token using the application's client
   credentials, over a POST that never appears in a URL or log.
5. ``GET /users/@me`` with that bearer token identifies the user, who is recorded and
   given a signed session cookie.
6. The access token is then discarded. Nothing afterwards needs it, and storing a
   credential with no use for it would be pure liability.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, DiscordServiceError
from app.core.logging import get_logger
from app.database.models import DiscordUser
from app.database.repositories.user_repository import UserRepository
from app.discord.rest_client import DiscordRestClient

logger = get_logger(__name__)

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


class OAuthNotConfiguredError(ConfigurationError):
    """Discord login was attempted without the required configuration."""

    code = "OAUTH_NOT_CONFIGURED"
    http_status = 503


class OAuthFailedError(DiscordServiceError):
    """The login could not be completed."""

    code = "OAUTH_FAILED"
    http_status = 400


@dataclass(slots=True)
class LoginRedirect:
    """Where to send the browser, and the state to remember while it is away."""

    url: str
    state: str


class DiscordOAuthService:
    """Builds the authorize URL and completes the callback."""

    def __init__(
        self,
        settings: Settings,
        rest_client: DiscordRestClient,
        repository: UserRepository,
    ) -> None:
        self._settings = settings
        self._rest = rest_client
        self._repo = repository

    def _require_configured(self) -> None:
        if not self._settings.oauth_enabled:
            raise OAuthNotConfiguredError(
                "Discord login is not configured. Set DISCORD_APPLICATION_ID, "
                "DISCORD_CLIENT_SECRET and SESSION_SECRET."
            )

    def build_login_redirect(self) -> LoginRedirect:
        """Step 1: the URL that sends the user to Discord."""

        self._require_configured()
        state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": self._settings.discord_application_id,
                "redirect_uri": self._settings.discord_oauth_redirect_uri,
                "response_type": "code",
                "scope": " ".join(self._settings.oauth_scope_list),
                "state": state,
                "prompt": "consent",
            }
        )
        logger.info(
            "Starting Discord login",
            extra={"scopes": self._settings.oauth_scope_list},
        )
        return LoginRedirect(url=f"{AUTHORIZE_URL}?{query}", state=state)

    @staticmethod
    def verify_state(returned: str | None, expected: str | None) -> None:
        """Step 3: the CSRF check. Both values must be present and identical."""

        if not returned or not expected:
            raise OAuthFailedError(
                "Login state is missing or expired. Start the login again.",
                details={"reason": "MISSING_STATE"},
            )
        if not secrets.compare_digest(returned, expected):
            logger.warning("Rejected an OAuth callback with a mismatched state")
            raise OAuthFailedError(
                "Login state did not match. Start the login again.",
                details={"reason": "STATE_MISMATCH"},
            )

    async def complete_login(self, code: str) -> DiscordUser:
        """Steps 4-6: exchange the code, identify the user, record the login."""

        self._require_configured()

        try:
            token_response = await self._rest.exchange_oauth_code(
                code=code,
                redirect_uri=self._settings.discord_oauth_redirect_uri,
                client_id=self._settings.discord_application_id,
                client_secret=self._settings.client_secret,
            )
        except DiscordServiceError as exc:
            raise OAuthFailedError(
                "Discord rejected the login. The code may have expired -- try again.",
                details={"reason": "CODE_EXCHANGE_FAILED"},
            ) from exc

        access_token = token_response.get("access_token")
        if not access_token:
            raise OAuthFailedError(
                "Discord did not return an access token.",
                details={"reason": "NO_ACCESS_TOKEN"},
            )

        try:
            profile: dict[str, Any] = await self._rest.get_oauth_user(access_token)
        except DiscordServiceError as exc:
            raise OAuthFailedError(
                "Could not read the Discord profile for this login.",
                details={"reason": "PROFILE_FETCH_FAILED"},
            ) from exc
        finally:
            # The token has served its only purpose. Hand it back to Discord rather
            # than leaving a live credential floating around.
            await self._rest.revoke_oauth_token(
                token=access_token,
                client_id=self._settings.discord_application_id,
                client_secret=self._settings.client_secret,
            )

        user_id = str(profile.get("id") or "")
        if not user_id:
            raise OAuthFailedError(
                "Discord profile had no user id.", details={"reason": "NO_USER_ID"}
            )

        avatar = profile.get("avatar")
        user = await self._repo.record_login(
            discord_user_id=user_id,
            username=profile.get("username"),
            global_name=profile.get("global_name"),
            avatar_url=(
                f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
                if avatar
                else None
            ),
        )
        logger.info(
            "Discord login succeeded",
            extra={
                "discord_user_id": user_id,
                "username": user.username,
                "login_count": user.login_count,
            },
        )
        return user

    def session_payload(self, user: DiscordUser) -> dict[str, Any]:
        """What goes inside the signed cookie. Readable by the browser: no secrets."""

        return {
            "sub": user.discord_user_id,
            "username": user.username,
            "global_name": user.global_name,
            "avatar_url": user.avatar_url,
        }
