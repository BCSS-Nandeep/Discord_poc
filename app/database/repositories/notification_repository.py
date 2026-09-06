"""Persistence for application-level notifications."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select

from app.database.models import DiscordNotification, utcnow
from app.database.repositories.base import BaseRepository


class NotificationRepository(BaseRepository):
    """CRUD for ``discord_notifications``."""

    async def create(
        self,
        *,
        event_type: str,
        message: str,
        access_request_id: int | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        payload_json: str | None = None,
        created_at: datetime | None = None,
    ) -> DiscordNotification:
        notification = DiscordNotification(
            event_type=event_type,
            message=message,
            access_request_id=access_request_id,
            guild_id=guild_id,
            channel_id=channel_id,
            payload_json=payload_json,
            created_at=created_at or utcnow(),
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def get(self, notification_id: int) -> DiscordNotification | None:
        result = await self.session.execute(
            select(DiscordNotification).where(DiscordNotification.id == notification_id)
        )
        return result.scalar_one_or_none()

    async def list_notifications(
        self,
        *,
        channel_id: str | None = None,
        guild_id: str | None = None,
        event_type: str | None = None,
        access_request_id: int | None = None,
        unread: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[DiscordNotification], int]:
        stmt = select(DiscordNotification)
        count_stmt = select(func.count()).select_from(DiscordNotification)

        conditions = []
        if channel_id is not None:
            conditions.append(DiscordNotification.channel_id == channel_id)
        if guild_id is not None:
            conditions.append(DiscordNotification.guild_id == guild_id)
        if event_type is not None:
            conditions.append(DiscordNotification.event_type == event_type)
        if access_request_id is not None:
            conditions.append(DiscordNotification.access_request_id == access_request_id)
        if unread is True:
            conditions.append(DiscordNotification.read_at.is_(None))
        elif unread is False:
            conditions.append(DiscordNotification.read_at.is_not(None))
        if since is not None:
            conditions.append(DiscordNotification.created_at >= since)
        if until is not None:
            conditions.append(DiscordNotification.created_at <= until)

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = int((await self.session.execute(count_stmt)).scalar_one())
        result = await self.session.execute(
            stmt.order_by(DiscordNotification.created_at.desc(), DiscordNotification.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total

    async def mark_read(
        self, notification: DiscordNotification, *, now: datetime | None = None
    ) -> DiscordNotification:
        notification.read_at = now or utcnow()
        await self.session.flush()
        return notification

    async def mark_delivered(
        self, notification: DiscordNotification, *, now: datetime | None = None
    ) -> DiscordNotification:
        notification.delivered_at = now or utcnow()
        await self.session.flush()
        return notification

    async def count_unread(self) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(DiscordNotification)
            .where(DiscordNotification.read_at.is_(None))
        )
        return int(result.scalar_one())
