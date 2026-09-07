"""Persistence for people who log in with Discord."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select

from app.database.models import DiscordUser, utcnow
from app.database.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """CRUD for ``discord_users``."""

    async def get_by_discord_id(self, discord_user_id: str) -> DiscordUser | None:
        result = await self.session.execute(
            select(DiscordUser).where(DiscordUser.discord_user_id == discord_user_id)
        )
        return result.scalar_one_or_none()

    async def record_login(
        self,
        *,
        discord_user_id: str,
        username: str | None,
        global_name: str | None,
        avatar_url: str | None,
        now: datetime | None = None,
    ) -> DiscordUser:
        """Create or refresh a user on successful login."""

        moment = now or utcnow()
        user = await self.get_by_discord_id(discord_user_id)
        if user is None:
            user = DiscordUser(discord_user_id=discord_user_id, login_count=0)
            self.session.add(user)
        user.username = username
        user.global_name = global_name
        user.avatar_url = avatar_url
        user.last_login_at = moment
        user.login_count += 1
        user.updated_at = moment
        await self.session.flush()
        return user

    async def list_users(
        self, *, limit: int = 50, offset: int = 0
    ) -> Sequence[DiscordUser]:
        result = await self.session.execute(
            select(DiscordUser)
            .order_by(DiscordUser.last_login_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(DiscordUser))
        return int(result.scalar_one())
