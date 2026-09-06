"""Keyword search over messages already stored in SQLite.

This never calls Discord.  Live keyword filtering during collection is handled by
:mod:`app.discord.keywords`; this service queries what was collected.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.logging import get_logger
from app.database.models import DiscordMessage
from app.database.repositories.message_repository import MessageRepository
from app.discord.keywords import KeywordConfig, KeywordMatcher, normalize_keywords

logger = get_logger(__name__)


@dataclass(slots=True)
class SearchHit:
    """A stored message plus the keywords that matched this particular query."""

    message: DiscordMessage
    matched_keywords: list[str]


class SearchService:
    """Searches stored messages by keyword and metadata."""

    def __init__(self, repository: MessageRepository) -> None:
        self._repo = repository

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
        match_mode: str = "substring",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SearchHit], int]:
        """Search stored messages, annotating each hit with its matched keywords.

        SQL ``LIKE`` does the coarse filtering (so paging happens in the database), then
        the shared :class:`KeywordMatcher` recomputes exact matches per row.  For
        ``word``/``exact`` modes that second pass also discards rows that ``LIKE``
        matched too loosely.
        """

        cleaned = normalize_keywords(keywords or [])

        rows, total = await self._repo.search(
            keywords=cleaned or None,
            channel_id=channel_id,
            guild_id=guild_id,
            author_id=author_id,
            since=since,
            until=until,
            match_all=match_all,
            case_sensitive=case_sensitive,
            limit=limit,
            offset=offset,
        )

        if not cleaned:
            return [SearchHit(message=row, matched_keywords=[]) for row in rows], total

        matcher = KeywordMatcher(
            KeywordConfig(
                keywords=cleaned,
                match_mode=match_mode,  # type: ignore[arg-type]
                case_sensitive=case_sensitive,
            )
        )

        hits: list[SearchHit] = []
        for row in rows:
            matched = matcher.match(row.content)
            if match_mode != "substring" and not matched:
                # LIKE was a superset for word/exact modes; drop the false positives.
                continue
            if match_all and len(matched) < len(cleaned):
                continue
            hits.append(SearchHit(message=row, matched_keywords=matched))

        logger.info(
            "Stored-message search completed",
            extra={
                "keywords": len(cleaned),
                "channel_id": channel_id,
                "hits": len(hits),
                "total_candidates": total,
            },
        )
        return hits, total

    @staticmethod
    def stored_keywords(message: DiscordMessage) -> list[str]:
        """Keywords recorded at collection time (may differ from a later query)."""

        if not message.matched_keywords_json:
            return []
        try:
            data = json.loads(message.matched_keywords_json)
        except (ValueError, TypeError):
            return []
        return [str(item) for item in data] if isinstance(data, list) else []
