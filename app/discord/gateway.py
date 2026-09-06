"""Discord Gateway listener for real-time events.

Built on discord.py's official Gateway client (bot token only -- never a user token and
never a self-bot).  It does two jobs:

**Live message collection.**  ``on_message`` persists messages for channels with a
RUNNING monitor, applying that monitor's keyword configuration.

**Fast-path permission signals.**  ``on_guild_channel_update`` /
``on_guild_role_update`` / ``on_member_update`` mean a channel's permissions may have
changed, so any PENDING access request for that channel is re-checked immediately
instead of waiting up to 12 hours.  The reconciliation worker remains the authoritative
periodic consistency check.

Every event handler opens its own database session.  Gateway callbacks run on
discord.py's event loop and must never share a session with an HTTP request.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from app.core.config import Settings
from app.core.logging import get_logger, register_secret

logger = get_logger(__name__)

#: Passed to the permission-signal handler when a guild-wide change (a role edit, or
#: the bot's own roles changing) means every pending request could be affected.
RECHECK_ALL_PENDING = "*"

#: Called with a channel id -- or ``RECHECK_ALL_PENDING`` -- on a permission change.
PermissionSignalHandler = Callable[[str], Awaitable[Any]]
#: Called with a raw message payload for a monitored channel.
MessageHandler = Callable[[dict[str, Any], str, str | None], Awaitable[Any]]


def build_intents(settings: Settings) -> discord.Intents:
    """Intents this service needs.

    ``guilds`` powers channel/permission events.  ``guild_messages`` delivers new
    messages.  ``message_content`` is privileged and must be enabled in the Discord
    Developer Portal, otherwise message text arrives empty.
    """

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = settings.discord_message_content_intent
    intents.members = settings.discord_guild_members_intent
    return intents


class DiscordGatewayClient(discord.Client):
    """discord.py client wired to this service's handlers."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_monitored_message: MessageHandler,
        on_permission_signal: PermissionSignalHandler,
        monitored_channels: Callable[[], set[str]],
    ) -> None:
        super().__init__(intents=build_intents(settings))
        self._settings = settings
        self._on_message_cb = on_monitored_message
        self._on_permission_cb = on_permission_signal
        self._monitored_channels = monitored_channels

    async def on_ready(self) -> None:  # pragma: no cover - requires a live Gateway
        user = self.user
        logger.info(
            "Gateway connected",
            extra={
                "bot_id": str(user.id) if user else None,
                "bot_name": str(user) if user else None,
                "guild_count": len(self.guilds),
            },
        )

    async def on_resumed(self) -> None:  # pragma: no cover - requires a live Gateway
        logger.info("Gateway session resumed")

    async def on_disconnect(self) -> None:  # pragma: no cover - requires a live Gateway
        logger.warning("Gateway disconnected; discord.py will attempt to reconnect")

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        logger.exception("Unhandled Gateway event error", extra={"event": event_method})

    async def on_message(self, message: discord.Message) -> None:
        """Persist a live message when its channel is actively monitored."""

        try:
            channel_id = str(message.channel.id)
            if channel_id not in self._monitored_channels():
                return
            if self.user is not None and message.author.id == self.user.id:
                return  # never collect our own messages

            guild_id = str(message.guild.id) if message.guild else None
            payload = self._serialize_message(message)
            await self._on_message_cb(payload, channel_id, guild_id)
        except Exception:  # noqa: BLE001 - a bad event must not kill the Gateway
            logger.exception(
                "Failed to process Gateway message",
                extra={"channel_id": str(message.channel.id)},
            )

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        """A channel's overwrites may have changed -- re-check access early."""

        try:
            if before.overwrites == after.overwrites:
                return
            logger.info(
                "Gateway saw a channel permission change",
                extra={"channel_id": str(after.id), "guild_id": str(after.guild.id)},
            )
            await self._on_permission_cb(str(after.id))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to handle channel update event")

    async def on_guild_channel_create(
        self, channel: discord.abc.GuildChannel
    ) -> None:  # pragma: no cover - requires a live Gateway
        logger.info(
            "Gateway saw a new channel",
            extra={"channel_id": str(channel.id), "guild_id": str(channel.guild.id)},
        )

    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        """A role's permissions changed; every pending channel may be affected."""

        try:
            if before.permissions == after.permissions:
                return
            logger.info(
                "Gateway saw a role permission change",
                extra={"guild_id": str(after.guild.id), "role_id": str(after.id)},
            )
            await self._on_permission_cb(RECHECK_ALL_PENDING)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to handle role update event")

    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """The bot's own roles changed, which can grant or remove channel access."""

        try:
            if self.user is None or after.id != self.user.id:
                return
            if before.roles == after.roles:
                return
            logger.info(
                "Gateway saw the bot's roles change",
                extra={"guild_id": str(after.guild.id)},
            )
            await self._on_permission_cb(RECHECK_ALL_PENDING)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to handle member update event")

    @staticmethod
    def _serialize_message(message: discord.Message) -> dict[str, Any]:
        """Convert a discord.py Message into the same shape the REST API returns.

        Feeding both paths the identical dict means one normalization routine and one
        set of behaviours for stored messages.
        """

        return {
            "id": str(message.id),
            "type": int(message.type.value),
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "author": {
                "id": str(message.author.id),
                "username": message.author.name,
                "global_name": getattr(message.author, "global_name", None),
                "bot": bool(message.author.bot),
            },
            "content": message.content or "",
            "timestamp": message.created_at.isoformat(),
            "edited_timestamp": (
                message.edited_at.isoformat() if message.edited_at else None
            ),
            "pinned": bool(message.pinned),
            "tts": bool(message.tts),
            "mention_everyone": bool(message.mention_everyone),
            "attachments": [
                {
                    "id": str(item.id),
                    "filename": item.filename,
                    "size": item.size,
                    "content_type": item.content_type,
                    "url": item.url,
                }
                for item in message.attachments
            ],
            "embeds": [embed.to_dict() for embed in message.embeds],
            "message_reference": (
                {"message_id": str(message.reference.message_id)}
                if message.reference and message.reference.message_id
                else None
            ),
        }


class GatewayService:
    """Owns the Gateway connection lifecycle and the monitored-channel cache."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_monitored_message: MessageHandler,
        on_permission_signal: PermissionSignalHandler,
    ) -> None:
        self._settings = settings
        self._on_message = on_monitored_message
        self._on_permission = on_permission_signal
        self._client: DiscordGatewayClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._monitored: set[str] = set()
        self._status: str = "disabled"
        self._last_error: str | None = None
        register_secret(settings.bot_token)

    # ---------------------------------------------------------------- monitor set --
    def set_monitored_channels(self, channel_ids: set[str]) -> None:
        """Replace the set of channels whose live messages should be collected."""

        self._monitored = {str(cid) for cid in channel_ids}
        logger.info(
            "Gateway monitored-channel set updated",
            extra={"monitored_count": len(self._monitored)},
        )

    def add_monitored_channel(self, channel_id: str) -> None:
        self._monitored.add(str(channel_id))

    def remove_monitored_channel(self, channel_id: str) -> None:
        self._monitored.discard(str(channel_id))

    def monitored_channels(self) -> set[str]:
        return self._monitored

    # ----------------------------------------------------------------- lifecycle --
    @property
    def status(self) -> str:
        """One of ``disabled``, ``starting``, ``connected``, ``stopped``, ``error``."""

        if self._client is not None and self._client.is_ready():
            return "connected"
        return self._status

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Connect to the Gateway in a background task (idempotent)."""

        if not self._settings.enable_gateway:
            self._status = "disabled"
            logger.info("Gateway is disabled by configuration")
            return
        if not self._settings.discord_configured:
            self._status = "error"
            self._last_error = "DISCORD_BOT_TOKEN is not configured"
            logger.error("Cannot start Gateway: bot token is not configured")
            return
        if self.is_running:
            logger.debug("Gateway already running; start() ignored")
            return

        self._client = DiscordGatewayClient(
            self._settings,
            on_monitored_message=self._on_message,
            on_permission_signal=self._on_permission,
            monitored_channels=self.monitored_channels,
        )
        self._status = "starting"
        self._last_error = None
        self._task = asyncio.create_task(self._run(), name="discord-gateway")
        logger.info("Gateway worker started")

    async def _run(self) -> None:
        assert self._client is not None
        try:
            await self._client.start(self._settings.bot_token)
        except asyncio.CancelledError:
            raise
        except discord.PrivilegedIntentsRequired as exc:
            self._status = "error"
            self._last_error = (
                "A privileged intent is not enabled for this application. Enable "
                "Message Content (and Server Members if required) in the Discord "
                "Developer Portal, or set DISCORD_MESSAGE_CONTENT_INTENT=false."
            )
            logger.error(
                "Gateway refused: privileged intents are not enabled",
                extra={"error": str(exc)},
            )
        except discord.LoginFailure:
            self._status = "error"
            self._last_error = "Discord rejected the bot token."
            logger.error("Gateway login failed: Discord rejected the bot token")
        except Exception as exc:  # noqa: BLE001 - never let this kill the app
            self._status = "error"
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Gateway worker crashed")
        else:
            self._status = "stopped"

    async def stop(self) -> None:
        """Close the Gateway connection and await the worker task."""

        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("Error while closing the Gateway client")
        if self._task is not None:
            self._task.cancel()
            # Shutdown is best effort: a failing task must not block the app closing.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._client = None
        if self._status != "error":
            self._status = "stopped"
        logger.info("Gateway worker stopped")

    def snapshot(self) -> dict[str, Any]:
        """Status summary for the health endpoint. Never includes the token."""

        return {
            "enabled": self._settings.enable_gateway,
            "status": self.status,
            "monitored_channels": len(self._monitored),
            "last_error": self._last_error,
            "message_content_intent": self._settings.discord_message_content_intent,
        }
