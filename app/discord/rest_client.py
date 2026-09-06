"""The single abstraction over the Discord REST API.

Nothing else in this service is allowed to construct an HTTP request to Discord.  That
keeps authentication, the pinned API version, rate-limit handling, retry policy and
error translation in exactly one place.

Rate limits
-----------
Discord returns per-route bucket headers (``X-RateLimit-Bucket``, ``-Remaining``,
``-Reset-After``).  This client tracks, per bucket, the moment it is safe to send the
next request and waits proactively rather than provoking a 429.  If a 429 does happen
(shared buckets, other processes using the same token) the client honours
``Retry-After`` for a bounded number of attempts.

Retries
-------
Only transient failures are retried: 429, 5xx, timeouts and connection errors.
Permission problems (401/403) and missing resources (404) raise immediately -- retrying
them would hammer Discord for a result that cannot change.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    DiscordAPIError,
    DiscordForbiddenError,
    DiscordNotConfiguredError,
    DiscordNotFoundError,
    DiscordRateLimitError,
    DiscordServerError,
    DiscordTransportError,
    DiscordUnauthorizedError,
)
from app.core.logging import get_logger, register_secret

logger = get_logger(__name__)

#: Discord caps this endpoint at 100 messages per call.
MAX_MESSAGE_PAGE_SIZE: Final[int] = 100

HttpMethod = Literal["GET", "POST", "PATCH", "PUT", "DELETE"]


class _Bucket:
    """Tracks when the next request against a rate-limit bucket may be sent."""

    __slots__ = ("lock", "reset_at")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reset_at: float = 0.0


def validate_snowflake(value: str | int, *, field: str = "id") -> str:
    """Validate a Discord snowflake and return it as a string.

    Every id that reaches a URL passes through here, so a caller can never inject a
    path segment or steer this client at an arbitrary endpoint.
    """

    from app.core.exceptions import ValidationError

    text = str(value).strip()
    if not text.isdigit() or not (1 <= len(text) <= 20):
        raise ValidationError(
            f"Invalid Discord {field}: must be a numeric snowflake",
            details={"field": field},
        )
    return text


class DiscordRestClient:
    """Async client for the official Discord Bot REST API."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._buckets: dict[str, _Bucket] = {}
        self._route_to_bucket: dict[str, str] = {}
        self._global_reset_at: float = 0.0
        register_secret(settings.bot_token)

    # ------------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        """Create the underlying HTTP client (idempotent)."""

        if self._client is not None:
            return
        if not self._settings.discord_configured:
            logger.error(
                "DISCORD_BOT_TOKEN is not set; Discord API calls will be rejected"
            )
        headers = {
            "User-Agent": self._settings.discord_user_agent,
            "Accept": "application/json",
        }
        if self._settings.discord_configured:
            # The only place the token is ever used.
            headers["Authorization"] = f"Bot {self._settings.bot_token}"
        self._client = httpx.AsyncClient(
            base_url=self._settings.discord_api_base_url,
            headers=headers,
            timeout=httpx.Timeout(self._settings.discord_request_timeout_seconds),
            transport=self._transport,
            follow_redirects=False,
        )
        logger.info(
            "Discord REST client initialised",
            extra={"base_url": self._settings.discord_api_base_url},
        )

    async def close(self) -> None:
        """Close the HTTP client."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Discord REST client closed")

    async def __aenter__(self) -> DiscordRestClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -------------------------------------------------------------- rate limiting --
    def _bucket_for(self, route_key: str) -> _Bucket:
        bucket_hash = self._route_to_bucket.get(route_key, route_key)
        bucket = self._buckets.get(bucket_hash)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[bucket_hash] = bucket
        return bucket

    async def _wait_for_slot(self, bucket: _Bucket) -> None:
        now = time.monotonic()
        wait_until = max(bucket.reset_at, self._global_reset_at)
        if wait_until > now:
            delay = wait_until - now
            logger.debug("Waiting for rate-limit slot", extra={"delay_seconds": delay})
            await asyncio.sleep(delay)

    def _update_bucket(self, route_key: str, response: httpx.Response) -> None:
        headers = response.headers
        bucket_hash = headers.get("X-RateLimit-Bucket")
        if bucket_hash:
            self._route_to_bucket[route_key] = bucket_hash
        bucket = self._bucket_for(route_key)

        remaining = headers.get("X-RateLimit-Remaining")
        reset_after = headers.get("X-RateLimit-Reset-After")
        if remaining is None or reset_after is None:
            return
        try:
            if int(remaining) <= 0:
                bucket.reset_at = time.monotonic() + float(reset_after)
        except ValueError:  # pragma: no cover - malformed header
            return

    # ------------------------------------------------------------------- requests --
    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        route_key: str | None = None,
    ) -> Any:
        """Perform an authenticated Discord API call with retries and rate limiting.

        ``path`` must be a service-constructed, already validated path -- never a URL
        supplied by an API client.
        """

        if self._client is None:
            await self.start()
        if not self._settings.discord_configured:
            raise DiscordNotConfiguredError(
                "Discord bot token is not configured; set DISCORD_BOT_TOKEN"
            )
        assert self._client is not None

        key = route_key or f"{method}:{path}"
        bucket = self._bucket_for(key)
        attempts = self._settings.discord_max_retries + 1
        last_error: DiscordAPIError | None = None

        for attempt in range(1, attempts + 1):
            async with bucket.lock:
                await self._wait_for_slot(bucket)
            try:
                response = await self._client.request(
                    method, path, params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                last_error = DiscordTransportError(
                    "Timed out talking to Discord", details={"path": path}
                )
                logger.warning(
                    "Discord request timed out",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
            except httpx.HTTPError as exc:
                last_error = DiscordTransportError(
                    "Network error talking to Discord", details={"path": path}
                )
                logger.warning(
                    "Discord request failed at transport level",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
            else:
                self._update_bucket(key, response)

                if response.status_code == 429:
                    retry_after, is_global = self._parse_retry_after(response)
                    logger.warning(
                        "Discord rate limited this request",
                        extra={
                            "path": path,
                            "retry_after": retry_after,
                            "global": is_global,
                            "attempt": attempt,
                        },
                    )
                    if retry_after > self._settings.discord_max_rate_limit_wait_seconds:
                        raise DiscordRateLimitError(
                            "Discord rate limit retry window is longer than allowed",
                            retry_after=retry_after,
                            is_global=is_global,
                        )
                    if is_global:
                        self._global_reset_at = time.monotonic() + retry_after
                    else:
                        bucket.reset_at = time.monotonic() + retry_after
                    last_error = DiscordRateLimitError(
                        "Discord rate limit exceeded",
                        retry_after=retry_after,
                        is_global=is_global,
                    )
                    if attempt < attempts:
                        await asyncio.sleep(retry_after)
                        continue
                    raise last_error

                if response.status_code >= 500:
                    last_error = DiscordServerError(
                        "Discord returned a server error",
                        status_code=response.status_code,
                    )
                    logger.warning(
                        "Discord server error",
                        extra={
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt,
                        },
                    )
                elif response.status_code >= 400:
                    raise self._translate_client_error(response, path)
                else:
                    return self._decode(response)

            if attempt < attempts:
                await asyncio.sleep(self._backoff_delay(attempt))

        assert last_error is not None
        raise last_error

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, clamped to the configured maximum."""

        base = self._settings.discord_retry_base_delay_seconds
        delay = min(
            base * (2 ** (attempt - 1)), self._settings.discord_max_retry_delay_seconds
        )
        return delay * (0.5 + random.random() / 2)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> tuple[float, bool]:
        retry_after = 1.0
        is_global = response.headers.get("X-RateLimit-Global", "").lower() == "true"
        try:
            body = response.json()
            if isinstance(body, dict):
                retry_after = float(body.get("retry_after", retry_after))
                is_global = bool(body.get("global", is_global))
        except (ValueError, TypeError):
            header = response.headers.get("Retry-After")
            if header:
                with contextlib.suppress(ValueError):
                    retry_after = float(header)
        return max(retry_after, 0.0), is_global

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - Discord always returns JSON
            raise DiscordAPIError(
                "Discord returned a non-JSON response",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _translate_client_error(response: httpx.Response, path: str) -> DiscordAPIError:
        """Map a 4xx into a typed, non-retryable exception."""

        discord_code: int | None = None
        message = "Discord rejected the request"
        try:
            body = response.json()
            if isinstance(body, dict):
                discord_code = body.get("code")
                message = str(body.get("message") or message)
        except ValueError:
            pass

        status = response.status_code
        details = {"path": path}
        if status == 401:
            logger.error("Discord rejected the bot token (401)", extra={"path": path})
            return DiscordUnauthorizedError(
                "Discord rejected the bot token",
                status_code=status,
                discord_code=discord_code,
                details=details,
            )
        if status == 403:
            return DiscordForbiddenError(
                message, status_code=status, discord_code=discord_code, details=details
            )
        if status == 404:
            return DiscordNotFoundError(
                message, status_code=status, discord_code=discord_code, details=details
            )
        return DiscordAPIError(
            message, status_code=status, discord_code=discord_code, details=details
        )

    # ------------------------------------------------------------------ endpoints --
    async def get_current_bot(self) -> dict[str, Any]:
        """``GET /users/@me`` -- identity and health check for the bot token."""

        return await self.request("GET", "/users/@me", route_key="GET:/users/@me")

    async def get_current_application(self) -> dict[str, Any]:
        """``GET /applications/@me`` -- includes the configured gateway intent flags."""

        return await self.request(
            "GET", "/applications/@me", route_key="GET:/applications/@me"
        )

    async def get_guilds(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """``GET /users/@me/guilds`` -- every guild the bot has been invited to."""

        pages: list[dict[str, Any]] = []
        after: str | None = None
        page_size = min(max(limit, 1), 200)
        while len(pages) < limit:
            params: dict[str, Any] = {"limit": min(page_size, 200)}
            if after:
                params["after"] = after
            batch = await self.request(
                "GET",
                "/users/@me/guilds",
                params=params,
                route_key="GET:/users/@me/guilds",
            )
            if not batch:
                break
            pages.extend(batch)
            if len(batch) < params["limit"]:
                break
            after = str(batch[-1]["id"])
        return pages[:limit]

    async def get_guild(self, guild_id: str) -> dict[str, Any]:
        """``GET /guilds/{guild_id}``."""

        gid = validate_snowflake(guild_id, field="guild_id")
        return await self.request(
            "GET", f"/guilds/{quote(gid)}", route_key="GET:/guilds/{guild_id}"
        )

    async def get_guild_channels(self, guild_id: str) -> list[dict[str, Any]]:
        """``GET /guilds/{guild_id}/channels``."""

        gid = validate_snowflake(guild_id, field="guild_id")
        return await self.request(
            "GET",
            f"/guilds/{quote(gid)}/channels",
            route_key="GET:/guilds/{guild_id}/channels",
        )

    async def get_guild_roles(self, guild_id: str) -> list[dict[str, Any]]:
        """``GET /guilds/{guild_id}/roles`` -- used for permission computation."""

        gid = validate_snowflake(guild_id, field="guild_id")
        return await self.request(
            "GET",
            f"/guilds/{quote(gid)}/roles",
            route_key="GET:/guilds/{guild_id}/roles",
        )

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict[str, Any]:
        """``GET /guilds/{guild_id}/members/{user_id}``.

        Requires the privileged Server Members intent for the bot's application.
        """

        gid = validate_snowflake(guild_id, field="guild_id")
        uid = validate_snowflake(user_id, field="user_id")
        return await self.request(
            "GET",
            f"/guilds/{quote(gid)}/members/{quote(uid)}",
            route_key="GET:/guilds/{guild_id}/members/{user_id}",
        )

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        """``GET /channels/{channel_id}``.

        A 403 here is the authoritative signal that the bot cannot view the channel.
        """

        cid = validate_snowflake(channel_id, field="channel_id")
        return await self.request(
            "GET", f"/channels/{quote(cid)}", route_key="GET:/channels/{channel_id}"
        )

    async def get_channel_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
        around: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /channels/{channel_id}/messages`` -- one page of history.

        Discord returns at most 100 messages per call; pagination is the caller's job
        (see :class:`app.discord.message_service.MessageService`).
        """

        cid = validate_snowflake(channel_id, field="channel_id")
        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_MESSAGE_PAGE_SIZE))}
        if before is not None:
            params["before"] = validate_snowflake(before, field="before")
        if after is not None:
            params["after"] = validate_snowflake(after, field="after")
        if around is not None:
            params["around"] = validate_snowflake(around, field="around")
        return await self.request(
            "GET",
            f"/channels/{quote(cid)}/messages",
            params=params,
            route_key="GET:/channels/{channel_id}/messages",
        )
