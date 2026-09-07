"""Persistence for discovered guilds."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from app.database.models import DiscordGuild, utcnow
from app.database.repositories.base import BaseRepository


class GuildRepository(BaseRepository):
    """CRUD for ``discord_guilds``."""

    async def get_by_guild_id(self, guild_id: str) -> DiscordGuild | None:
        result = await self.session.execute(
            select(DiscordGuild).where(DiscordGuild.guild_id == guild_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 200, offset: int = 0) -> Sequence[DiscordGuild]:
        result = await self.session.execute(
            select(DiscordGuild).order_by(DiscordGuild.name).limit(limit).offset(offset)
        )
        return result.scalars().all()

    async def upsert(
        self,
        *,
        guild_id: str,
        name: str | None = None,
        icon_url: str | None = None,
        owner_id: str | None = None,
        is_available: bool = True,
    ) -> DiscordGuild:
        """Insert or update a guild row, returning the persisted entity."""

        guild = await self.get_by_guild_id(guild_id)
        if guild is None:
            guild = DiscordGuild(guild_id=guild_id)
            self.session.add(guild)
        guild.name = name if name is not None else guild.name
        guild.icon_url = icon_url if icon_url is not None else guild.icon_url
        guild.owner_id = owner_id if owner_id is not None else guild.owner_id
        guild.is_available = is_available
        guild.updated_at = utcnow()
        await self.session.flush()
        return guild

    async def set_monitoring_enabled(
        self, guild_id: str, *, enabled: bool
    ) -> DiscordGuild | None:
        """Flip the guild-level monitoring master switch. ``None`` if unknown locally."""

        guild = await self.get_by_guild_id(guild_id)
        if guild is None:
            return None
        guild.monitoring_enabled = enabled
        guild.updated_at = utcnow()
        await self.session.flush()
        return guild

    async def search_by_name(
        self, query: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[Sequence[DiscordGuild], int]:
        """Case-insensitive substring search over stored guild names.

        Only guilds the bot has been invited to are searchable -- Discord provides no
        way for a bot to discover servers it is not a member of.
        """

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = func.lower(DiscordGuild.name).like(f"%{escaped.lower()}%", escape="\\")

        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(DiscordGuild).where(condition)
                )
            ).scalar_one()
        )
        result = await self.session.execute(
            select(DiscordGuild)
            .where(condition)
            .order_by(DiscordGuild.name)
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(DiscordGuild))
        return int(result.scalar_one())
