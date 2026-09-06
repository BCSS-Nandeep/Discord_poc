"""Historical message collection and persistence.

Discord returns at most 100 messages per ``GET /channels/{id}/messages`` call, so a
channel is walked backwards in pages using the ``before`` cursor.  Duplicate protection
is delegated to the database's unique constraint, which makes a re-scrape cheap and
safe.

Collection only ever runs for a channel the bot can actually read; the caller
(:class:`app.discord.access_request_service.AccessWorkflowService`) is responsible for
confirming access first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings
from app.core.exceptions import (
    DiscordAPIError,
    DiscordForbiddenError,
    DiscordNotFoundError,
    DiscordRateLimitError,
)
from app.core.logging import get_logger
from app.database.models import DiscordMessage
from app.database.repositories.message_repository import MessageRepository
from app.discord.keywords import KeywordConfig, KeywordMatcher
from app.discord.normalize import normalize_message
from app.discord.rest_client import (
    MAX_MESSAGE_PAGE_SIZE,
    DiscordRestClient,
    validate_snowflake,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class ScrapeResult:
    """Outcome of one historical collection run."""

    channel_id: str
    guild_id: str | None = None
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    matched: int = 0
    skipped_non_matching: int = 0
    pages: int = 0
    oldest_message_id: str | None = None
    newest_message_id: str | None = None
    completed: bool = True
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


class MessageService:
    """Fetches, normalizes, filters and stores Discord messages."""

    def __init__(
        self,
        rest_client: DiscordRestClient,
        repository: MessageRepository,
        settings: Settings,
    ) -> None:
        self._rest = rest_client
        self._repo = repository
        self._settings = settings

    # ----------------------------------------------------------------- collection --
    async def collect_history(
        self,
        channel_id: str,
        *,
        guild_id: str | None = None,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
        keyword_config: KeywordConfig | None = None,
        page_size: int | None = None,
        store_raw: bool = True,
    ) -> ScrapeResult:
        """Page backwards through a channel's history and store what it finds.

        ``limit`` bounds the total number of messages fetched (not per page).  ``after``
        makes the run incremental: paging stops once the cursor reaches that message.
        """

        cid = validate_snowflake(channel_id, field="channel_id")
        total_limit = limit if limit is not None else self._settings.history_max_messages
        total_limit = max(1, min(int(total_limit), 1_000_000))
        page_size = min(
            page_size or self._settings.history_page_size, MAX_MESSAGE_PAGE_SIZE
        )

        matcher = KeywordMatcher(keyword_config or KeywordConfig())
        result = ScrapeResult(channel_id=cid, guild_id=guild_id)
        cursor = validate_snowflake(before, field="before") if before else None
        stop_at = validate_snowflake(after, field="after") if after else None

        logger.info(
            "Historical collection started",
            extra={
                "channel_id": cid,
                "guild_id": guild_id,
                "limit": total_limit,
                "page_size": page_size,
                "keywords": len(matcher.config.keywords),
            },
        )

        while result.fetched < total_limit:
            remaining = total_limit - result.fetched
            batch_size = min(page_size, remaining)
            try:
                batch = await self._rest.get_channel_messages(
                    cid, limit=batch_size, before=cursor
                )
            except DiscordForbiddenError as exc:
                # Access was revoked mid-run. Stop cleanly and report it.
                result.completed = False
                result.stopped_reason = "FORBIDDEN"
                result.errors.append("Discord denied access to this channel.")
                logger.warning(
                    "Historical collection stopped: access denied",
                    extra={"channel_id": cid, "discord_code": exc.discord_code},
                )
                break
            except DiscordNotFoundError:
                result.completed = False
                result.stopped_reason = "NOT_FOUND"
                result.errors.append("Channel no longer exists.")
                break
            except DiscordRateLimitError as exc:
                result.completed = False
                result.stopped_reason = "RATE_LIMITED"
                result.errors.append(
                    f"Rate limited by Discord (retry after {exc.retry_after}s)."
                )
                logger.warning(
                    "Historical collection stopped: rate limited",
                    extra={"channel_id": cid, "retry_after": exc.retry_after},
                )
                break
            except DiscordAPIError as exc:
                result.completed = False
                result.stopped_reason = "DISCORD_ERROR"
                result.errors.append("Discord API error during collection.")
                logger.warning(
                    "Historical collection stopped: Discord error",
                    extra={"channel_id": cid, "status": exc.status_code},
                )
                break

            if not batch:
                break

            result.pages += 1
            result.fetched += len(batch)

            rows, page_stats = self._prepare_rows(
                batch,
                channel_id=cid,
                guild_id=guild_id,
                matcher=matcher,
                store_raw=store_raw,
                stop_at=stop_at,
            )
            result.matched += page_stats["matched"]
            result.skipped_non_matching += page_stats["skipped"]

            if rows:
                inserted = await self._repo.bulk_insert_ignore_duplicates(rows)
                result.stored += inserted
                result.duplicates += len(rows) - inserted

            last_id = str(batch[-1]["id"])
            first_id = str(batch[0]["id"])
            result.newest_message_id = result.newest_message_id or first_id
            result.oldest_message_id = last_id
            cursor = last_id

            if page_stats["reached_stop"]:
                result.stopped_reason = "REACHED_AFTER_CURSOR"
                break
            if len(batch) < batch_size:
                # Discord returned a short page: the channel history is exhausted.
                break

        logger.info(
            "Historical collection finished",
            extra={
                "channel_id": cid,
                "fetched": result.fetched,
                "stored": result.stored,
                "duplicates": result.duplicates,
                "matched": result.matched,
                "pages": result.pages,
                "completed": result.completed,
                "stopped_reason": result.stopped_reason,
            },
        )
        return result

    def _prepare_rows(
        self,
        batch: list[dict],
        *,
        channel_id: str,
        guild_id: str | None,
        matcher: KeywordMatcher,
        store_raw: bool,
        stop_at: str | None,
    ) -> tuple[list[dict], dict[str, int | bool]]:
        """Normalize a page, apply keyword filtering and honour the ``after`` cursor."""

        rows: list[dict] = []
        matched_count = 0
        skipped = 0
        reached_stop = False

        for payload in batch:
            message_id = str(payload.get("id"))
            if stop_at is not None and int(message_id) <= int(stop_at):
                reached_stop = True
                continue

            matched = matcher.match(payload.get("content"))
            if matched:
                matched_count += 1
            if not matcher.should_store(matched):
                skipped += 1
                continue

            rows.append(
                normalize_message(
                    payload,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    matched_keywords=matched,
                    source="rest",
                    store_raw=store_raw,
                )
            )

        return rows, {
            "matched": matched_count,
            "skipped": skipped,
            "reached_stop": reached_stop,
        }

    async def store_live_message(
        self,
        payload: dict,
        *,
        channel_id: str,
        guild_id: str | None,
        matched_keywords: list[str],
        store_raw: bool = True,
    ) -> bool:
        """Persist one Gateway message. Returns ``True`` when it was newly stored."""

        row = normalize_message(
            payload,
            channel_id=channel_id,
            guild_id=guild_id,
            matched_keywords=matched_keywords,
            source="gateway",
            store_raw=store_raw,
        )
        inserted = await self._repo.bulk_insert_ignore_duplicates([row])
        return inserted > 0

    # --------------------------------------------------------------------- reading --
    async def list_stored_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        before: datetime | None = None,
        after: datetime | None = None,
    ) -> tuple[Sequence[DiscordMessage], int]:
        cid = validate_snowflake(channel_id, field="channel_id")
        rows = await self._repo.list_for_channel(
            cid, limit=limit, offset=offset, before=before, after=after
        )
        total = await self._repo.count_for_channel(cid)
        return rows, total

    async def newest_stored_message_id(self, channel_id: str) -> str | None:
        return await self._repo.newest_message_id(str(channel_id))

    async def count(self) -> int:
        return await self._repo.count()
