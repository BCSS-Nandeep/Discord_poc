"""Persistence for live channel monitors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select

from app.core.enums import MonitorStatus
from app.database.models import DiscordMonitor, utcnow
from app.database.repositories.base import BaseRepository

#: Statuses the Gateway should keep an eye on when it restores state at startup.
ACTIVE_STATUSES = (
    MonitorStatus.RUNNING.value,
    MonitorStatus.STARTING.value,
    MonitorStatus.WAITING_FOR_ACCESS.value,
)


class MonitorRepository(BaseRepository):
    """CRUD for ``discord_monitors``."""

    async def get(self, monitor_id: int) -> DiscordMonitor | None:
        result = await self.session.execute(
            select(DiscordMonitor).where(DiscordMonitor.id == monitor_id)
        )
        return result.scalar_one_or_none()

    async def get_by_channel_id(self, channel_id: str) -> DiscordMonitor | None:
        result = await self.session.execute(
            select(DiscordMonitor).where(DiscordMonitor.channel_id == channel_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self, *, channel_id: str, guild_id: str | None
    ) -> tuple[DiscordMonitor, bool]:
        """Return ``(monitor, created)`` for a channel."""

        monitor = await self.get_by_channel_id(channel_id)
        if monitor is not None:
            if guild_id is not None and monitor.guild_id != guild_id:
                monitor.guild_id = guild_id
            return monitor, False
        monitor = DiscordMonitor(
            channel_id=channel_id,
            guild_id=guild_id,
            status=MonitorStatus.STOPPED.value,
        )
        self.session.add(monitor)
        await self.session.flush()
        return monitor, True

    async def list_monitors(
        self,
        *,
        status: str | None = None,
        guild_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[DiscordMonitor]:
        stmt = select(DiscordMonitor)
        if status is not None:
            stmt = stmt.where(DiscordMonitor.status == status)
        if guild_id is not None:
            stmt = stmt.where(DiscordMonitor.guild_id == guild_id)
        result = await self.session.execute(
            stmt.order_by(DiscordMonitor.id.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all()

    async def list_active(self) -> Sequence[DiscordMonitor]:
        """Monitors that should be restored when the service restarts."""

        result = await self.session.execute(
            select(DiscordMonitor).where(DiscordMonitor.status.in_(ACTIVE_STATUSES))
        )
        return result.scalars().all()

    async def list_running_channel_ids(self) -> list[str]:
        result = await self.session.execute(
            select(DiscordMonitor.channel_id).where(
                DiscordMonitor.status == MonitorStatus.RUNNING.value
            )
        )
        return list(result.scalars().all())

    async def set_status(
        self,
        monitor: DiscordMonitor,
        *,
        status: MonitorStatus,
        now: datetime | None = None,
        last_error: str | None = None,
    ) -> DiscordMonitor:
        """Apply a validated status change. Validation lives in the MonitorService."""

        moment = now or utcnow()
        monitor.status = status.value
        monitor.updated_at = moment
        if status is MonitorStatus.RUNNING:
            monitor.started_at = monitor.started_at or moment
            monitor.stopped_at = None
            monitor.last_error = None
        elif status is MonitorStatus.STOPPED:
            monitor.stopped_at = moment
        if last_error is not None:
            monitor.last_error = last_error
        await self.session.flush()
        return monitor

    async def record_event(
        self,
        monitor: DiscordMonitor,
        *,
        message_id: str,
        occurred_at: datetime,
        matched: bool,
    ) -> DiscordMonitor:
        """Update live counters after a Gateway message was processed."""

        monitor.last_event_at = occurred_at
        monitor.last_message_id = message_id
        monitor.messages_seen += 1
        if matched:
            monitor.messages_matched += 1
        monitor.updated_at = occurred_at
        await self.session.flush()
        return monitor

    async def set_keyword_config(
        self, monitor: DiscordMonitor, *, keyword_config_json: str | None
    ) -> DiscordMonitor:
        monitor.keyword_config_json = keyword_config_json
        monitor.updated_at = utcnow()
        await self.session.flush()
        return monitor

    async def count(self, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(DiscordMonitor)
        if status is not None:
            stmt = stmt.where(DiscordMonitor.status == status)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())
