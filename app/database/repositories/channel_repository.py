"""Persistence for channels and their evaluated access state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from app.database.models import DiscordChannel, utcnow
from app.database.repositories.base import BaseRepository


class ChannelRepository(BaseRepository):
    """CRUD for ``discord_channels``."""

    async def get_by_channel_id(self, channel_id: str) -> DiscordChannel | None:
        result = await self.session.execute(
            select(DiscordChannel).where(DiscordChannel.channel_id == channel_id)
        )
        return result.scalar_one_or_none()

    async def list_for_guild(
        self,
        guild_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
        access_status: str | None = None,
        is_private: bool | None = None,
    ) -> Sequence[DiscordChannel]:
        stmt = select(DiscordChannel).where(DiscordChannel.guild_id == guild_id)
        if access_status is not None:
            stmt = stmt.where(DiscordChannel.access_status == access_status)
        if is_private is not None:
            stmt = stmt.where(DiscordChannel.is_private.is_(is_private))
        stmt = stmt.order_by(DiscordChannel.position, DiscordChannel.name)
        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return result.scalars().all()

    async def upsert_metadata(
        self,
        *,
        channel_id: str,
        guild_id: str | None = None,
        parent_id: str | None = None,
        name: str | None = None,
        channel_type: int | None = None,
        position: int | None = None,
        topic: str | None = None,
    ) -> DiscordChannel:
        """Create or refresh the descriptive metadata of a channel."""

        channel = await self.get_by_channel_id(channel_id)
        if channel is None:
            channel = DiscordChannel(channel_id=channel_id)
            self.session.add(channel)
        if guild_id is not None:
            channel.guild_id = guild_id
        if parent_id is not None:
            channel.parent_id = parent_id
        if name is not None:
            channel.name = name
        if channel_type is not None:
            channel.channel_type = channel_type
        if position is not None:
            channel.position = position
        if topic is not None:
            channel.topic = topic
        channel.updated_at = utcnow()
        await self.session.flush()
        return channel

    async def update_access_state(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        is_private: bool,
        bot_can_view: bool,
        bot_can_read_history: bool,
        bot_can_read_message_content: bool,
        access_status: str,
        access_reason: str | None,
        checked_at: datetime | None = None,
    ) -> DiscordChannel:
        """Write the outcome of a permission evaluation onto the channel row."""

        channel = await self.get_by_channel_id(channel_id)
        if channel is None:
            channel = DiscordChannel(channel_id=channel_id)
            self.session.add(channel)
        if guild_id is not None:
            channel.guild_id = guild_id
        channel.is_private = is_private
        channel.bot_can_view = bot_can_view
        channel.bot_can_read_history = bot_can_read_history
        channel.bot_can_read_message_content = bot_can_read_message_content
        channel.access_status = access_status
        channel.access_reason = access_reason
        channel.last_permission_check_at = checked_at or utcnow()
        channel.updated_at = utcnow()
        await self.session.flush()
        return channel

    async def search_by_name(
        self,
        query: str,
        *,
        guild_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[DiscordChannel], int]:
        """Case-insensitive substring search over stored channel names.

        Returns ``(rows, total_matches)``. LIKE wildcards in the query are escaped so a
        caller cannot turn the search into a pattern match.
        """

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions: list[ColumnElement[bool]] = [
            func.lower(DiscordChannel.name).like(f"%{escaped.lower()}%", escape="\\")
        ]
        if guild_id is not None:
            conditions.append(DiscordChannel.guild_id == guild_id)

        stmt = select(DiscordChannel)
        count_stmt = select(func.count()).select_from(DiscordChannel)
        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = int((await self.session.execute(count_stmt)).scalar_one())
        result = await self.session.execute(
            stmt.order_by(DiscordChannel.guild_id, DiscordChannel.position, DiscordChannel.name)
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(DiscordChannel))
        return int(result.scalar_one())
