"""Background historical-collection jobs.

Mirrors the fire-and-poll pattern for long-running collection: creating a job returns
immediately with ``QUEUED``, and the caller polls ``GET .../scrape/jobs/{id}`` for
progress instead of holding an HTTP connection open for however long a large channel
takes to page through.

``ScrapeJobService`` owns the job record and its state transitions.  The actual paging
happens in :meth:`app.discord.client.DiscordClientManager.run_scrape_job`, which opens
its own database session -- a job outlives the HTTP request that queued it, so it must
never share that request's session (see the Gateway handlers in ``client.py`` for the
same pattern).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.enums import ScrapeJobStatus
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import DiscordScrapeJob
from app.database.repositories.scrape_job_repository import ScrapeJobRepository
from app.discord.keywords import KeywordConfig
from app.discord.message_service import ScrapeResult
from app.discord.rest_client import validate_snowflake

logger = get_logger(__name__)


class ScrapeJobService:
    """Creates, tracks and reports on background scrape jobs."""

    def __init__(self, repository: ScrapeJobRepository) -> None:
        self._repo = repository

    async def create(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        limit: int | None,
        before: str | None,
        after: str | None,
        incremental: bool,
        keyword_config: KeywordConfig | None,
    ) -> DiscordScrapeJob:
        cid = validate_snowflake(channel_id, field="channel_id")
        job = await self._repo.create(
            channel_id=cid,
            guild_id=guild_id,
            requested_limit=limit,
            before_cursor=before,
            after_cursor=after,
            incremental=incremental,
            keyword_config_json=(
                keyword_config.to_json() if keyword_config and keyword_config.enabled else None
            ),
        )
        logger.info(
            "Scrape job queued",
            extra={"job_id": job.id, "channel_id": cid, "limit": limit},
        )
        return job

    async def get(self, job_id: int) -> DiscordScrapeJob:
        job = await self._repo.get(job_id)
        if job is None:
            raise NotFoundError("Scrape job not found", details={"job_id": job_id})
        return job

    async def list_for_channel(
        self, channel_id: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[DiscordScrapeJob], int]:
        cid = validate_snowflake(channel_id, field="channel_id")
        rows = await self._repo.list_for_channel(cid, limit=limit, offset=offset)
        total = await self._repo.count_for_channel(cid)
        return rows, total

    async def mark_running(self, job: DiscordScrapeJob) -> DiscordScrapeJob:
        return await self._repo.mark_running(job)

    async def record_progress(self, job: DiscordScrapeJob, result: ScrapeResult) -> None:
        await self._repo.update_progress(
            job,
            fetched=result.fetched,
            stored=result.stored,
            duplicates=result.duplicates,
            matched=result.matched,
            pages=result.pages,
            checkpoint_message_id=result.oldest_message_id,
        )

    async def mark_completed(self, job: DiscordScrapeJob, result: ScrapeResult) -> DiscordScrapeJob:
        await self.record_progress(job, result)
        status = ScrapeJobStatus.COMPLETED if result.completed else ScrapeJobStatus.PARTIAL
        return await self._repo.mark_finished(
            job, status=status, stopped_reason=result.stopped_reason
        )

    async def mark_failed(self, job: DiscordScrapeJob, *, error_message: str) -> DiscordScrapeJob:
        logger.warning(
            "Scrape job failed", extra={"job_id": job.id, "error": error_message}
        )
        return await self._repo.mark_finished(
            job, status=ScrapeJobStatus.FAILED, error_message=error_message
        )
