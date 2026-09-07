"""Persistence for background historical-collection jobs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select

from app.core.enums import ScrapeJobStatus
from app.database.models import DiscordScrapeJob, utcnow
from app.database.repositories.base import BaseRepository


class ScrapeJobRepository(BaseRepository):
    """CRUD for ``discord_scrape_jobs``."""

    async def create(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        requested_limit: int | None,
        before_cursor: str | None,
        after_cursor: str | None,
        incremental: bool,
        keyword_config_json: str | None,
    ) -> DiscordScrapeJob:
        job = DiscordScrapeJob(
            channel_id=channel_id,
            guild_id=guild_id,
            status=ScrapeJobStatus.QUEUED.value,
            requested_limit=requested_limit,
            before_cursor=before_cursor,
            after_cursor=after_cursor,
            incremental=incremental,
            keyword_config_json=keyword_config_json,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: int) -> DiscordScrapeJob | None:
        result = await self.session.execute(
            select(DiscordScrapeJob).where(DiscordScrapeJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def list_for_channel(
        self, channel_id: str, *, limit: int = 20, offset: int = 0
    ) -> Sequence[DiscordScrapeJob]:
        result = await self.session.execute(
            select(DiscordScrapeJob)
            .where(DiscordScrapeJob.channel_id == channel_id)
            .order_by(DiscordScrapeJob.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count_for_channel(self, channel_id: str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(DiscordScrapeJob)
            .where(DiscordScrapeJob.channel_id == channel_id)
        )
        return int(result.scalar_one())

    async def mark_running(
        self, job: DiscordScrapeJob, *, now: datetime | None = None
    ) -> DiscordScrapeJob:
        moment = now or utcnow()
        job.status = ScrapeJobStatus.RUNNING.value
        job.started_at = moment
        job.updated_at = moment
        await self.session.flush()
        return job

    async def update_progress(
        self,
        job: DiscordScrapeJob,
        *,
        fetched: int,
        stored: int,
        duplicates: int,
        matched: int,
        pages: int,
        checkpoint_message_id: str | None,
    ) -> DiscordScrapeJob:
        """Persist cumulative counters after a page, so a poll mid-run shows movement."""

        job.messages_fetched = fetched
        job.messages_stored = stored
        job.messages_duplicate = duplicates
        job.messages_matched = matched
        job.pages_fetched = pages
        if checkpoint_message_id is not None:
            job.checkpoint_message_id = checkpoint_message_id
        job.updated_at = utcnow()
        await self.session.flush()
        return job

    async def mark_finished(
        self,
        job: DiscordScrapeJob,
        *,
        status: ScrapeJobStatus,
        stopped_reason: str | None = None,
        error_message: str | None = None,
    ) -> DiscordScrapeJob:
        moment = utcnow()
        job.status = status.value
        job.stopped_reason = stopped_reason
        job.error_message = error_message
        job.finished_at = moment
        job.updated_at = moment
        await self.session.flush()
        return job
