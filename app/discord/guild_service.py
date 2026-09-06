"""Guild discovery and persistence."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import DiscordGuild
from app.database.repositories.guild_repository import GuildRepository
from app.discord.normalize import normalize_guild
from app.discord.rest_client import DiscordRestClient, validate_snowflake

logger = get_logger(__name__)


class GuildService:
    """Discovers the guilds the bot belongs to and mirrors them into SQLite."""

    def __init__(self, rest_client: DiscordRestClient, repository: GuildRepository) -> None:
        self._rest = rest_client
        self._repo = repository

    async def discover_guilds(self, *, limit: int = 200) -> list[DiscordGuild]:
        """Fetch every guild from Discord and upsert it locally."""

        payload = await self._rest.get_guilds(limit=limit)
        stored: list[DiscordGuild] = []
        for item in payload:
            normalized = normalize_guild(item)
            stored.append(await self._repo.upsert(**normalized))
        logger.info("Guild discovery completed", extra={"guild_count": len(stored)})
        return stored

    async def list_guilds(
        self, *, limit: int = 200, offset: int = 0, refresh: bool = False
    ) -> Sequence[DiscordGuild]:
        """List guilds from SQLite, optionally refreshing from Discord first."""

        if refresh:
            await self.discover_guilds(limit=limit)
        return await self._repo.list_all(limit=limit, offset=offset)

    async def get_guild(self, guild_id: str, *, refresh: bool = True) -> DiscordGuild:
        """Return one guild, refreshing it from Discord by default."""

        gid = validate_snowflake(guild_id, field="guild_id")
        if refresh:
            payload: dict[str, Any] = await self._rest.get_guild(gid)
            return await self._repo.upsert(**normalize_guild(payload))

        guild = await self._repo.get_by_guild_id(gid)
        if guild is None:
            raise NotFoundError(
                "Guild is not known to this service", details={"guild_id": gid}
            )
        return guild

    async def search_by_name(
        self, query: str, *, limit: int = 50, offset: int = 0, refresh: bool = False
    ) -> tuple[Sequence[DiscordGuild], int]:
        """Find guilds by name instead of snowflake id.

        Only guilds the bot has been invited to can be found: Discord provides no way
        for a bot to discover or search servers it is not a member of.
        """

        if refresh:
            await self.discover_guilds()

        rows, total = await self._repo.search_by_name(query, limit=limit, offset=offset)
        logger.info(
            "Guild name search",
            extra={"query_length": len(query), "matches": total},
        )
        return rows, total

    async def count(self) -> int:
        return await self._repo.count()
