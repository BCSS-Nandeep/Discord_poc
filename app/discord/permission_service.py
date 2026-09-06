"""Effective channel-access evaluation. The single source of truth for permissions.

Two independent signals are combined:

1. **Discord's own enforcement** (authoritative).  ``GET /channels/{id}`` returns 403
   when the bot lacks *View Channel*, and ``GET /channels/{id}/messages`` returns 403
   when it lacks *Read Message History*.  This needs no privileged intent and reflects
   exactly what a collection run would experience.

2. **Computed permission bits** (explanatory).  When the channel payload carries
   ``permission_overwrites`` we resolve them with Discord's documented algorithm to say
   *why* a channel is private and whether ``@everyone`` is denied *View Channel*.

Message content is a separate axis: the privileged *Message Content* intent gates the
``content`` field.  It is declared in configuration and, when possible, confirmed
empirically against fetched messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.core.enums import AccessReason, ChannelAccessStatus
from app.core.exceptions import (
    DiscordAPIError,
    DiscordForbiddenError,
    DiscordNotConfiguredError,
    DiscordNotFoundError,
    DiscordRateLimitError,
    DiscordUnauthorizedError,
)
from app.core.logging import get_logger
from app.database.models import utcnow
from app.discord.rest_client import DiscordRestClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------- permissions --
VIEW_CHANNEL = 1 << 10
READ_MESSAGE_HISTORY = 1 << 16
ADMINISTRATOR = 1 << 3

#: Discord channel types that can hold messages we are able to collect.
TEXT_CHANNEL_TYPES = frozenset(
    {
        0,  # GUILD_TEXT
        5,  # GUILD_ANNOUNCEMENT
        10,  # ANNOUNCEMENT_THREAD
        11,  # PUBLIC_THREAD
        12,  # PRIVATE_THREAD
        15,  # GUILD_FORUM
        16,  # GUILD_MEDIA
    }
)

CHANNEL_TYPE_NAMES: dict[int, str] = {
    0: "GUILD_TEXT",
    1: "DM",
    2: "GUILD_VOICE",
    3: "GROUP_DM",
    4: "GUILD_CATEGORY",
    5: "GUILD_ANNOUNCEMENT",
    10: "ANNOUNCEMENT_THREAD",
    11: "PUBLIC_THREAD",
    12: "PRIVATE_THREAD",
    13: "GUILD_STAGE_VOICE",
    14: "GUILD_DIRECTORY",
    15: "GUILD_FORUM",
    16: "GUILD_MEDIA",
}


def channel_type_name(channel_type: int | None) -> str:
    return CHANNEL_TYPE_NAMES.get(int(channel_type or 0), "UNKNOWN")


@dataclass(slots=True)
class AccessEvaluation:
    """Structured result of evaluating the bot's access to one channel."""

    channel_id: str
    guild_id: str | None = None
    channel_name: str | None = None
    channel_type: int | None = None
    parent_id: str | None = None
    position: int | None = None
    topic: str | None = None
    exists: bool = False
    is_private: bool = False
    bot_can_view: bool = False
    bot_can_read_history: bool = False
    bot_can_read_message_content: bool = False
    access_status: ChannelAccessStatus = ChannelAccessStatus.UNKNOWN
    reason: AccessReason = AccessReason.UNKNOWN
    detail: str | None = None
    transient: bool = False
    checked_at: datetime = field(default_factory=utcnow)
    raw_channel: dict[str, Any] | None = None

    @property
    def collection_allowed(self) -> bool:
        """Whether historical collection and monitoring may run right now."""

        return (
            self.exists
            and self.bot_can_view
            and self.bot_can_read_history
            and self.access_status
            in (ChannelAccessStatus.ACCESSIBLE, ChannelAccessStatus.PUBLIC_ACCESSIBLE)
        )

    def to_dict(self) -> dict[str, Any]:
        """The structured payload documented in the API schema."""

        return {
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "channel_name": self.channel_name,
            "channel_type": self.channel_type,
            "channel_type_name": channel_type_name(self.channel_type),
            "exists": self.exists,
            "is_private": self.is_private,
            "bot_can_view": self.bot_can_view,
            "bot_can_read_history": self.bot_can_read_history,
            "bot_can_read_message_content": self.bot_can_read_message_content,
            "access_status": self.access_status.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "collection_allowed": self.collection_allowed,
            "checked_at": self.checked_at,
        }


def compute_permissions(
    *,
    base_permissions: int,
    member_role_ids: list[str],
    member_id: str,
    guild_id: str,
    overwrites: list[dict[str, Any]],
) -> int:
    """Resolve channel permission overwrites per Discord's documented algorithm.

    Order: ``@everyone`` overwrite, then the union of role overwrites, then the
    member-specific overwrite. Administrators bypass all overwrites.
    """

    if base_permissions & ADMINISTRATOR:
        return ~0

    permissions = base_permissions
    by_id = {str(item.get("id")): item for item in overwrites}

    everyone = by_id.get(str(guild_id))
    if everyone:
        permissions &= ~int(everyone.get("deny", 0))
        permissions |= int(everyone.get("allow", 0))

    allow = 0
    deny = 0
    for role_id in member_role_ids:
        overwrite = by_id.get(str(role_id))
        if overwrite and int(overwrite.get("type", 0)) == 0:
            allow |= int(overwrite.get("allow", 0))
            deny |= int(overwrite.get("deny", 0))
    permissions &= ~deny
    permissions |= allow

    member = by_id.get(str(member_id))
    if member and int(member.get("type", 1)) == 1:
        permissions &= ~int(member.get("deny", 0))
        permissions |= int(member.get("allow", 0))

    return permissions


def everyone_denied_view(guild_id: str | None, overwrites: list[dict[str, Any]]) -> bool:
    """True when the ``@everyone`` role is denied *View Channel* on this channel.

    This is Discord's actual definition of a private channel: visibility is granted
    back to specific roles or members through additional overwrites.
    """

    if not guild_id:
        return False
    for overwrite in overwrites or []:
        if str(overwrite.get("id")) == str(guild_id):
            return bool(int(overwrite.get("deny", 0)) & VIEW_CHANNEL)
    return False


class PermissionService:
    """Evaluates the bot's effective access to a channel."""

    def __init__(self, rest_client: DiscordRestClient, settings: Settings) -> None:
        self._rest = rest_client
        self._settings = settings

    async def evaluate_channel_access(
        self, channel_id: str, *, probe_history: bool = True
    ) -> AccessEvaluation:
        """Determine the bot's effective access to ``channel_id``.

        Never raises for an expected Discord condition: a 403/404/429 is folded into the
        returned evaluation so callers get a decision rather than an exception.
        """

        evaluation = AccessEvaluation(channel_id=str(channel_id))

        try:
            channel = await self._rest.get_channel(channel_id)
        except DiscordNotFoundError:
            evaluation.exists = False
            evaluation.access_status = ChannelAccessStatus.UNKNOWN
            evaluation.reason = AccessReason.CHANNEL_NOT_FOUND
            evaluation.detail = "Channel does not exist or is not visible to this bot."
            logger.info("Channel not found", extra={"channel_id": channel_id})
            return evaluation
        except DiscordForbiddenError as exc:
            # Discord's own answer: the bot cannot see this channel.
            evaluation.exists = True
            evaluation.is_private = True
            evaluation.bot_can_view = False
            evaluation.access_status = ChannelAccessStatus.PRIVATE
            evaluation.reason = AccessReason.BOT_CANNOT_VIEW_CHANNEL
            evaluation.detail = (
                "The bot does not have the View Channel permission for this channel."
            )
            logger.info(
                "Channel is not viewable by the bot",
                extra={"channel_id": channel_id, "discord_code": exc.discord_code},
            )
            return evaluation
        except DiscordUnauthorizedError:
            evaluation.access_status = ChannelAccessStatus.ERROR
            evaluation.reason = AccessReason.INVALID_BOT_TOKEN
            evaluation.detail = "Discord rejected the bot token."
            return evaluation
        except DiscordNotConfiguredError:
            evaluation.access_status = ChannelAccessStatus.ERROR
            evaluation.reason = AccessReason.INVALID_BOT_TOKEN
            evaluation.detail = "Discord bot token is not configured."
            return evaluation
        except DiscordRateLimitError as exc:
            evaluation.access_status = ChannelAccessStatus.UNKNOWN
            evaluation.reason = AccessReason.RATE_LIMITED
            evaluation.transient = True
            evaluation.detail = (
                f"Rate limited by Discord; retry in {exc.retry_after or 'a moment'}s."
            )
            return evaluation
        except DiscordAPIError as exc:
            evaluation.access_status = ChannelAccessStatus.ERROR
            evaluation.reason = AccessReason.DISCORD_ERROR
            evaluation.transient = exc.retryable
            evaluation.detail = "Discord API error while reading the channel."
            logger.warning(
                "Discord error while evaluating channel",
                extra={"channel_id": channel_id, "status": exc.status_code},
            )
            return evaluation

        self._apply_channel_payload(evaluation, channel)

        if evaluation.channel_type is not None and (
            evaluation.channel_type not in TEXT_CHANNEL_TYPES
        ):
            evaluation.access_status = ChannelAccessStatus.ERROR
            evaluation.reason = AccessReason.NOT_A_TEXT_CHANNEL
            evaluation.detail = (
                f"Channel type {channel_type_name(evaluation.channel_type)} does not "
                "hold collectable messages."
            )
            return evaluation

        if probe_history:
            await self._probe_history(evaluation)
        else:
            evaluation.bot_can_read_history = True

        evaluation.bot_can_read_message_content = (
            self._settings.discord_message_content_intent
        )

        self._finalize(evaluation)
        return evaluation

    def _apply_channel_payload(
        self, evaluation: AccessEvaluation, channel: dict[str, Any]
    ) -> None:
        """Copy channel metadata onto the evaluation and derive privacy."""

        evaluation.exists = True
        evaluation.bot_can_view = True
        evaluation.raw_channel = channel
        evaluation.guild_id = (
            str(channel["guild_id"]) if channel.get("guild_id") is not None else None
        )
        evaluation.channel_name = channel.get("name")
        evaluation.channel_type = int(channel.get("type", 0))
        evaluation.parent_id = (
            str(channel["parent_id"]) if channel.get("parent_id") is not None else None
        )
        evaluation.position = channel.get("position")
        evaluation.topic = channel.get("topic")

        overwrites = channel.get("permission_overwrites") or []
        evaluation.is_private = everyone_denied_view(evaluation.guild_id, overwrites)
        if evaluation.channel_type == 12:  # PRIVATE_THREAD
            evaluation.is_private = True

    async def _probe_history(self, evaluation: AccessEvaluation) -> None:
        """Ask Discord directly whether message history can be read."""

        try:
            messages = await self._rest.get_channel_messages(
                evaluation.channel_id, limit=1
            )
        except DiscordForbiddenError:
            evaluation.bot_can_read_history = False
            return
        except DiscordNotFoundError:
            evaluation.bot_can_read_history = False
            evaluation.exists = False
            evaluation.reason = AccessReason.CHANNEL_NOT_FOUND
            return
        except DiscordRateLimitError:
            # Do not claim the permission is missing when we simply could not check.
            evaluation.bot_can_read_history = False
            evaluation.transient = True
            evaluation.reason = AccessReason.RATE_LIMITED
            evaluation.access_status = ChannelAccessStatus.UNKNOWN
            evaluation.detail = "Rate limited while checking message history access."
            return
        except DiscordAPIError as exc:
            evaluation.bot_can_read_history = False
            evaluation.transient = exc.retryable
            evaluation.reason = AccessReason.DISCORD_ERROR
            evaluation.access_status = ChannelAccessStatus.ERROR
            evaluation.detail = "Discord API error while checking message history."
            return

        evaluation.bot_can_read_history = True
        # Empirical confirmation: a non-empty payload whose messages all have empty
        # content is the signature of a missing Message Content intent.
        if messages and all(not (item.get("content") or "") for item in messages):
            if any(item.get("attachments") or item.get("embeds") for item in messages):
                return
            evaluation.detail = (
                "Message history is readable but returned no content; verify the "
                "Message Content intent is enabled."
            )

    def _finalize(self, evaluation: AccessEvaluation) -> None:
        """Decide the final access status from the gathered signals."""

        if evaluation.transient:
            return
        if evaluation.access_status is ChannelAccessStatus.ERROR:
            return

        if not evaluation.bot_can_read_history:
            evaluation.access_status = ChannelAccessStatus.PRIVATE
            evaluation.reason = AccessReason.MISSING_READ_MESSAGE_HISTORY
            evaluation.detail = (
                "The bot can see this channel but lacks the Read Message History "
                "permission."
            )
            return

        if not evaluation.bot_can_read_message_content:
            evaluation.access_status = ChannelAccessStatus.PRIVATE
            evaluation.reason = AccessReason.MESSAGE_CONTENT_NOT_ENABLED
            evaluation.detail = (
                "The privileged Message Content intent is not enabled for this "
                "application, so message text cannot be collected."
            )
            return

        # ACCESSIBLE means "private, but an administrator has granted the bot access";
        # PUBLIC_ACCESSIBLE means the channel is visible to @everyone. Both allow
        # collection -- the distinction is reported for operators, not enforcement.
        evaluation.access_status = (
            ChannelAccessStatus.ACCESSIBLE
            if evaluation.is_private
            else ChannelAccessStatus.PUBLIC_ACCESSIBLE
        )
        evaluation.reason = AccessReason.FULLY_ACCESSIBLE
        evaluation.detail = "The bot can read this channel and its message history."

    async def evaluate_guild_permissions(
        self, guild_id: str, channel: dict[str, Any], bot_user_id: str
    ) -> int | None:
        """Compute the bot's effective permission bits for a channel.

        Returns ``None`` when the data needed is unavailable (for example the Server
        Members intent is disabled, so the bot's member object cannot be fetched).
        Used for explanation only -- never as the authoritative access decision.
        """

        try:
            roles = await self._rest.get_guild_roles(guild_id)
            member = await self._rest.get_guild_member(guild_id, bot_user_id)
        except (DiscordAPIError, DiscordNotConfiguredError) as exc:
            logger.debug(
                "Could not compute permission bits",
                extra={"guild_id": guild_id, "error": type(exc).__name__},
            )
            return None

        role_permissions = {str(role["id"]): int(role.get("permissions", 0)) for role in roles}
        member_role_ids = [str(role_id) for role_id in member.get("roles", [])]

        base = role_permissions.get(str(guild_id), 0)
        for role_id in member_role_ids:
            base |= role_permissions.get(role_id, 0)

        return compute_permissions(
            base_permissions=base,
            member_role_ids=member_role_ids,
            member_id=str(bot_user_id),
            guild_id=str(guild_id),
            overwrites=channel.get("permission_overwrites") or [],
        )
