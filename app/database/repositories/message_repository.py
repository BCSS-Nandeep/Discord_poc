"""Persistence and search for collected Discord messages."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import DiscordMessage
from app.database.repositories.base import BaseRepository


class MessageRepository(BaseRepository):
    """CRUD for ``discord_messages``.

    Duplicate protection is enforced by the database (``ux_discord_messages_identity``
    over ``guild_id, channel_id, message_id``) combined with ``INSERT ... ON CONFLICT DO
    NOTHING``, so re-scraping a channel is always safe and never raises.
    """

    async def bulk_insert_ignore_duplicates(
        self, rows: Iterable[dict[str, Any]]
    ) -> int:
        """Insert normalized message rows, skipping ones already stored.

        Returns the number of rows actually inserted.
        """

        payload = list(rows)
        if not payload:
            return 0

        stmt = (
            sqlite_insert(DiscordMessage)
            .values(payload)
            .on_conflict_do_nothing(
                index_elements=[
                    DiscordMessage.guild_id,
                    DiscordMessage.channel_id,
                    DiscordMessage.message_id,
                ]
            )
        )
        result = cast(CursorResult, await self.session.execute(stmt))
        await self.session.flush()
        return int(result.rowcount or 0)

    async def exists(self, *, guild_id: str, channel_id: str, message_id: str) -> bool:
        result = await self.session.execute(
            select(DiscordMessage.id)
            .where(DiscordMessage.guild_id == guild_id)
            .where(DiscordMessage.channel_id == channel_id)
            .where(DiscordMessage.message_id == message_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_by_message_id(self, message_id: str) -> DiscordMessage | None:
        result = await self.session.execute(
            select(DiscordMessage).where(DiscordMessage.message_id == message_id).limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_channel(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        before: datetime | None = None,
        after: datetime | None = None,
        newest_first: bool = True,
    ) -> Sequence[DiscordMessage]:
        stmt = select(DiscordMessage).where(DiscordMessage.channel_id == channel_id)
        if before is not None:
            stmt = stmt.where(DiscordMessage.timestamp < before)
        if after is not None:
            stmt = stmt.where(DiscordMessage.timestamp > after)
        order = DiscordMessage.timestamp.desc() if newest_first else DiscordMessage.timestamp
        result = await self.session.execute(stmt.order_by(order).limit(limit).offset(offset))
        return result.scalars().all()

    async def count_for_channel(self, channel_id: str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(DiscordMessage)
            .where(DiscordMessage.channel_id == channel_id)
        )
        return int(result.scalar_one())

    async def oldest_message_id(self, channel_id: str) -> str | None:
        result = await self.session.execute(
            select(DiscordMessage.message_id)
            .where(DiscordMessage.channel_id == channel_id)
            .order_by(DiscordMessage.timestamp.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def newest_message_id(self, channel_id: str) -> str | None:
        result = await self.session.execute(
            select(DiscordMessage.message_id)
            .where(DiscordMessage.channel_id == channel_id)
            .order_by(DiscordMessage.timestamp.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def search(
        self,
        *,
        keywords: Sequence[str] | None = None,
        channel_id: str | None = None,
        guild_id: str | None = None,
        author_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        match_all: bool = False,
        case_sensitive: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[DiscordMessage], int]:
        """Search stored messages, returning ``(rows, total_matches)``."""

        stmt = select(DiscordMessage)
        count_stmt = select(func.count()).select_from(DiscordMessage)

        conditions = []
        if channel_id is not None:
            conditions.append(DiscordMessage.channel_id == channel_id)
        if guild_id is not None:
            conditions.append(DiscordMessage.guild_id == guild_id)
        if author_id is not None:
            conditions.append(DiscordMessage.author_id == author_id)
        if since is not None:
            conditions.append(DiscordMessage.timestamp >= since)
        if until is not None:
            conditions.append(DiscordMessage.timestamp <= until)

        if keywords:
            column = (
                DiscordMessage.content
                if case_sensitive
                else func.lower(DiscordMessage.content)
            )
            clauses = []
            for keyword in keywords:
                needle = keyword if case_sensitive else keyword.lower()
                # ``like`` with escaped wildcards keeps user input from turning into a
                # pattern; this is a parameterized query, never string interpolation.
                escaped = (
                    needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                clauses.append(column.like(f"%{escaped}%", escape="\\"))
            if match_all:
                conditions.extend(clauses)
            else:
                conditions.append(or_(*clauses))

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = int((await self.session.execute(count_stmt)).scalar_one())
        result = await self.session.execute(
            stmt.order_by(DiscordMessage.timestamp.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(DiscordMessage))
        return int(result.scalar_one())
