"""Exception hierarchy for the Discord service.

Every exception carries a stable ``code`` plus an HTTP status so the API layer can
translate it into a sanitized JSON error without leaking internals or secrets.
"""

from __future__ import annotations

from typing import Any


class DiscordServiceError(Exception):
    """Base class for every error raised by this service."""

    code: str = "DISCORD_SERVICE_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str = "Discord service error",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        """Sanitized representation returned to API clients."""

        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(DiscordServiceError):
    """The service is missing or has invalid configuration."""

    code = "CONFIGURATION_ERROR"
    http_status = 500


class DiscordNotConfiguredError(ConfigurationError):
    """No usable bot token / application id is configured."""

    code = "DISCORD_NOT_CONFIGURED"
    http_status = 503


class ValidationError(DiscordServiceError):
    """A caller supplied an invalid value that Pydantic could not reject."""

    code = "VALIDATION_ERROR"
    http_status = 422


class NotFoundError(DiscordServiceError):
    """A requested local or Discord resource does not exist."""

    code = "NOT_FOUND"
    http_status = 404


class ConflictError(DiscordServiceError):
    """The request conflicts with current state (e.g. duplicate access request)."""

    code = "CONFLICT"
    http_status = 409


class InvalidStateTransitionError(ConflictError):
    """A state machine transition was rejected."""

    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class AccessNotGrantedError(DiscordServiceError):
    """An operation needs channel access the bot does not have yet."""

    code = "ACCESS_NOT_GRANTED"
    http_status = 409


# --------------------------------------------------------------------------------------
# Discord API errors
# --------------------------------------------------------------------------------------
class DiscordAPIError(DiscordServiceError):
    """Base class for failures returned by (or while talking to) the Discord API."""

    code = "DISCORD_API_ERROR"
    http_status = 502

    #: Whether retrying the same call could plausibly succeed.
    retryable: bool = False

    def __init__(
        self,
        message: str = "Discord API error",
        *,
        status_code: int | None = None,
        discord_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status_code = status_code
        self.discord_code = discord_code

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.status_code is not None:
            payload["discord_status"] = self.status_code
        if self.discord_code is not None:
            payload["discord_code"] = self.discord_code
        return payload


class DiscordUnauthorizedError(DiscordAPIError):
    """401 - the bot token is missing, revoked or malformed."""

    code = "DISCORD_UNAUTHORIZED"
    http_status = 502
    retryable = False


class DiscordForbiddenError(DiscordAPIError):
    """403 - the bot lacks the permission required for this resource."""

    code = "DISCORD_FORBIDDEN"
    http_status = 403
    retryable = False


class DiscordNotFoundError(DiscordAPIError):
    """404 - the guild/channel/message does not exist or is invisible to the bot."""

    code = "DISCORD_NOT_FOUND"
    http_status = 404
    retryable = False


class DiscordRateLimitError(DiscordAPIError):
    """429 - the rate limit was hit and retries were exhausted."""

    code = "DISCORD_RATE_LIMITED"
    http_status = 429
    retryable = True

    def __init__(
        self,
        message: str = "Discord rate limit exceeded",
        *,
        retry_after: float | None = None,
        is_global: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after
        self.is_global = is_global

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        payload["global"] = self.is_global
        return payload


class DiscordServerError(DiscordAPIError):
    """5xx returned by Discord."""

    code = "DISCORD_SERVER_ERROR"
    http_status = 502
    retryable = True


class DiscordTransportError(DiscordAPIError):
    """Network/timeout failure while talking to Discord."""

    code = "DISCORD_TRANSPORT_ERROR"
    http_status = 504
    retryable = True
