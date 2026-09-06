"""Channel discovery, metadata persistence and access-state refresh."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.enums import AccessReason, ChannelAccessStatus
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import DiscordChannel
from app.database.repositories.channel_repository import ChannelRepository
from app.discord.normalize import normalize_channel
from app.discord.permission_service import (
    TEXT_CHANNEL_TYPES,
    AccessEvaluation,
    PermissionService,
    everyone_denied_view,
)
from app.discord.rest_client import DiscordRestClient, validate_snowflake

logger = get_logger(__name__)


class ChannelService:
    """Discovers channels and keeps their stored access state current."""

    def __init__(
        self,
        rest_client: DiscordRestClient,
        repository: ChannelRepository,
        permission_service: PermissionService,
    ) -> None:
        self._rest = rest_client
        self._repo = repository
        self._permissions = permission_service

    # ------------------------------------------------------------------ discovery --
    async def discover_guild_channels(
        self, guild_id: str, *, evaluate_access: bool = False
    ) -> list[DiscordChannel]:
        """List a guild's channels from Discord and mirror them into SQLite.

        ``GET /guilds/{id}/channels`` returns the guild's channels along with their
        permission overwrites, so private channels can be flagged without a per-channel
        probe.  Set ``evaluate_access`` to additionally run the full permission
        evaluation for every text channel (one or two extra API calls each).
        """

        gid = validate_snowflake(guild_id, field="guild_id")
        payload = await self._rest.get_guild_channels(gid)

        stored: list[DiscordChannel] = []
        for item in payload:
            metadata = normalize_channel(item)
            metadata["guild_id"] = metadata["guild_id"] or gid
            channel = await self._repo.upsert_metadata(**metadata)

            overwrites = item.get("permission_overwrites") or []
            is_private = everyone_denied_view(gid, overwrites)
            if channel.access_status == ChannelAccessStatus.UNKNOWN.value:
                channel.is_private = is_private
                channel.access_status = (
                    ChannelAccessStatus.PRIVATE.value
                    if is_private
                    else ChannelAccessStatus.UNKNOWN.value
                )
            stored.append(channel)

        await self._repo.flush()
        logger.info(
            "Channel discovery completed",
            extra={"guild_id": gid, "channel_count": len(stored)},
        )

        if evaluate_access:
            for channel in stored:
                if channel.channel_type in TEXT_CHANNEL_TYPES:
                    await self.refresh_access(channel.channel_id)

        return stored

    async def list_channels(
        self,
        guild_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
        access_status: str | None = None,
        is_private: bool | None = None,
        refresh: bool = False,
    ) -> Sequence[DiscordChannel]:
        gid = validate_snowflake(guild_id, field="guild_id")
        if refresh:
            await self.discover_guild_channels(gid)
        return await self._repo.list_for_guild(
            gid,
            limit=limit,
            offset=offset,
            access_status=access_status,
            is_private=is_private,
        )

    async def search_by_name(
        self,
        query: str,
        *,
        guild_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> tuple[Sequence[DiscordChannel], int]:
        """Find channels by name instead of snowflake id.

        Searches channels already discovered into SQLite. Discord has no channel-name
        lookup endpoint, so a channel only becomes searchable once its guild has been
        discovered -- pass ``refresh=True`` with a ``guild_id`` to discover first.
        """

        if refresh and guild_id is not None:
            await self.discover_guild_channels(guild_id)

        rows, total = await self._repo.search_by_name(
            query, guild_id=guild_id, limit=limit, offset=offset
        )
        logger.info(
            "Channel name search",
            extra={"query_length": len(query), "guild_id": guild_id, "matches": total},
        )
        return rows, total

    async def get_stored_channel(self, channel_id: str) -> DiscordChannel:
        cid = validate_snowflake(channel_id, field="channel_id")
        channel = await self._repo.get_by_channel_id(cid)
        if channel is None:
            raise NotFoundError(
                "Channel is not known to this service", details={"channel_id": cid}
            )
        return channel

    async def find_stored_channel(self, channel_id: str) -> DiscordChannel | None:
        return await self._repo.get_by_channel_id(str(channel_id))

    # -------------------------------------------------------------- access refresh --
    async def refresh_access(self, channel_id: str) -> tuple[DiscordChannel, AccessEvaluation]:
        """Evaluate the bot's access to a channel and persist the outcome.

        A *transient* failure (rate limit, Discord 5xx) intentionally leaves the stored
        access state untouched: an outage must never look like a permission change.
        """

        cid = validate_snowflake(channel_id, field="channel_id")
        evaluation = await self._permissions.evaluate_channel_access(cid)

        if evaluation.transient:
            logger.info(
                "Skipping channel state write after a transient Discord failure",
                extra={"channel_id": cid, "reason": evaluation.reason.value},
            )
            existing = await self._repo.get_by_channel_id(cid)
            if existing is not None:
                return existing, evaluation
            channel = await self._repo.upsert_metadata(channel_id=cid)
            return channel, evaluation

        if evaluation.exists and evaluation.bot_can_view:
            await self._repo.upsert_metadata(
                channel_id=cid,
                guild_id=evaluation.guild_id,
                parent_id=evaluation.parent_id,
                name=evaluation.channel_name,
                channel_type=evaluation.channel_type,
                position=evaluation.position,
                topic=evaluation.topic,
            )

        channel = await self._repo.update_access_state(
            channel_id=cid,
            guild_id=evaluation.guild_id,
            is_private=evaluation.is_private,
            bot_can_view=evaluation.bot_can_view,
            bot_can_read_history=evaluation.bot_can_read_history,
            bot_can_read_message_content=evaluation.bot_can_read_message_content,
            access_status=evaluation.access_status.value,
            access_reason=evaluation.reason.value,
            checked_at=evaluation.checked_at,
        )
        logger.info(
            "Channel access evaluated",
            extra={
                "channel_id": cid,
                "guild_id": evaluation.guild_id,
                "access_status": evaluation.access_status.value,
                "reason": evaluation.reason.value,
            },
        )
        return channel, evaluation

    async def evaluate_only(self, channel_id: str) -> AccessEvaluation:
        """Evaluate access without writing to the database."""

        cid = validate_snowflake(channel_id, field="channel_id")
        return await self._permissions.evaluate_channel_access(cid)

    @staticmethod
    def is_collectable(evaluation: AccessEvaluation) -> bool:
        """Whether historical collection / monitoring may run for this evaluation."""

        return evaluation.collection_allowed and (
            evaluation.reason is AccessReason.FULLY_ACCESSIBLE
        )

    async def count(self) -> int:
        return await self._repo.count()
