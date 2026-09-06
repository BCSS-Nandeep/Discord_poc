"""Persistence for application-level private-channel access requests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, func, select, update

from app.core.enums import AccessRequestStatus
from app.database.models import DiscordAccessRequest, utcnow
from app.database.repositories.base import BaseRepository

#: Statuses that mean "this request is still waiting for an administrator".
OPEN_STATUSES = (AccessRequestStatus.PENDING.value,)


class AccessRequestRepository(BaseRepository):
    """CRUD plus the atomic claim used by the reconciliation worker."""

    async def get(self, request_id: int) -> DiscordAccessRequest | None:
        result = await self.session.execute(
            select(DiscordAccessRequest).where(DiscordAccessRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def get_open_for_channel(self, channel_id: str) -> DiscordAccessRequest | None:
        """Return the PENDING request for a channel, if one exists."""

        result = await self.session.execute(
            select(DiscordAccessRequest)
            .where(DiscordAccessRequest.channel_id == channel_id)
            .where(DiscordAccessRequest.status.in_(OPEN_STATUSES))
            .order_by(DiscordAccessRequest.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_for_channel(self, channel_id: str) -> DiscordAccessRequest | None:
        result = await self.session.execute(
            select(DiscordAccessRequest)
            .where(DiscordAccessRequest.channel_id == channel_id)
            .order_by(DiscordAccessRequest.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_requests(
        self,
        *,
        status: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[DiscordAccessRequest]:
        stmt = select(DiscordAccessRequest)
        if status is not None:
            stmt = stmt.where(DiscordAccessRequest.status == status)
        if guild_id is not None:
            stmt = stmt.where(DiscordAccessRequest.guild_id == guild_id)
        if channel_id is not None:
            stmt = stmt.where(DiscordAccessRequest.channel_id == channel_id)
        stmt = stmt.order_by(DiscordAccessRequest.id.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_requests(
        self,
        *,
        status: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(DiscordAccessRequest)
        if status is not None:
            stmt = stmt.where(DiscordAccessRequest.status == status)
        if guild_id is not None:
            stmt = stmt.where(DiscordAccessRequest.guild_id == guild_id)
        if channel_id is not None:
            stmt = stmt.where(DiscordAccessRequest.channel_id == channel_id)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def create(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        channel_name: str | None,
        requested_at: datetime,
        next_check_at: datetime,
        expires_at: datetime | None,
        collect_history_on_grant: bool = True,
        monitor_on_grant: bool = False,
        keyword_config_json: str | None = None,
        requested_by: str | None = None,
        note: str | None = None,
    ) -> DiscordAccessRequest:
        request = DiscordAccessRequest(
            channel_id=channel_id,
            guild_id=guild_id,
            channel_name=channel_name,
            requested_at=requested_at,
            status=AccessRequestStatus.PENDING.value,
            next_check_at=next_check_at,
            expires_at=expires_at,
            check_count=0,
            collect_history_on_grant=collect_history_on_grant,
            monitor_on_grant=monitor_on_grant,
            keyword_config_json=keyword_config_json,
            requested_by=requested_by,
            note=note,
        )
        self.session.add(request)
        await self.session.flush()
        return request

    async def list_due(
        self, *, now: datetime, limit: int = 25
    ) -> Sequence[DiscordAccessRequest]:
        """PENDING requests whose ``next_check_at`` has come due."""

        result = await self.session.execute(
            select(DiscordAccessRequest)
            .where(DiscordAccessRequest.status == AccessRequestStatus.PENDING.value)
            .where(DiscordAccessRequest.next_check_at <= now)
            .order_by(DiscordAccessRequest.next_check_at)
            .limit(limit)
        )
        return result.scalars().all()

    async def claim_for_check(
        self, *, request_id: int, now: datetime, next_check_at: datetime
    ) -> bool:
        """Atomically claim a request for checking.

        The ``WHERE`` clause is the concurrency guard: the row is only claimed if it is
        still PENDING and still due.  A second worker (or a manual re-check racing the
        scheduler) updates zero rows and skips the request, which is what makes the
        worker idempotent and free of duplicate processing.

        Pushing ``next_check_at`` forward as part of the claim also means a crash
        mid-check cannot produce a hot retry loop.
        """

        result = cast(
            CursorResult,
            await self.session.execute(
                update(DiscordAccessRequest)
                .where(DiscordAccessRequest.id == request_id)
                .where(DiscordAccessRequest.status == AccessRequestStatus.PENDING.value)
                .where(DiscordAccessRequest.next_check_at <= now)
                .values(
                    last_checked_at=now,
                    next_check_at=next_check_at,
                    check_count=DiscordAccessRequest.check_count + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        return await self._finish_claim(result.rowcount, request_id)

    async def force_claim(self, *, request_id: int, now: datetime) -> bool:
        """Claim for a manual re-check, ignoring ``next_check_at``."""

        result = cast(
            CursorResult,
            await self.session.execute(
                update(DiscordAccessRequest)
                .where(DiscordAccessRequest.id == request_id)
                .where(DiscordAccessRequest.status == AccessRequestStatus.PENDING.value)
                .values(
                    last_checked_at=now,
                    check_count=DiscordAccessRequest.check_count + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        return await self._finish_claim(result.rowcount, request_id)

    async def _finish_claim(self, rowcount: int | None, request_id: int) -> bool:
        """Report whether the claim succeeded, refreshing the ORM entity if it did.

        The claim is a bulk UPDATE with ``synchronize_session=False``, so SQLAlchemy
        does not know the row changed. Without this refresh a caller holding the entity
        would keep reading a stale ``check_count`` / ``next_check_at``.
        """

        if not rowcount:
            return False
        entity = await self.session.get(DiscordAccessRequest, request_id)
        if entity is not None:
            await self.session.refresh(entity)
        return True

    async def apply_status(
        self,
        request: DiscordAccessRequest,
        *,
        status: AccessRequestStatus,
        now: datetime,
        next_check_at: datetime | None = None,
        rejection_reason: str | None = None,
        last_error: str | None = None,
    ) -> DiscordAccessRequest:
        """Write a validated status transition. Validation happens in the service."""

        request.status = status.value
        request.last_checked_at = now
        request.updated_at = now
        request.next_check_at = next_check_at
        if status is AccessRequestStatus.ACCEPTED:
            request.accepted_at = now
            request.rejection_reason = None
            request.last_error = None
        elif status is AccessRequestStatus.DENIED:
            request.denied_at = now
            request.rejection_reason = rejection_reason
        else:
            if rejection_reason is not None:
                request.rejection_reason = rejection_reason
        if last_error is not None:
            request.last_error = last_error
        await self.session.flush()
        return request

    async def record_check_result(
        self,
        request: DiscordAccessRequest,
        *,
        now: datetime,
        next_check_at: datetime | None,
        last_error: str | None,
    ) -> DiscordAccessRequest:
        """Update diagnostics for a check that left the request PENDING."""

        request.last_checked_at = now
        request.next_check_at = next_check_at
        request.last_error = last_error
        request.updated_at = now
        await self.session.flush()
        return request

    async def list_expired_candidates(
        self, *, now: datetime, limit: int = 100
    ) -> Sequence[DiscordAccessRequest]:
        result = await self.session.execute(
            select(DiscordAccessRequest)
            .where(DiscordAccessRequest.status == AccessRequestStatus.PENDING.value)
            .where(DiscordAccessRequest.expires_at.is_not(None))
            .where(DiscordAccessRequest.expires_at <= now)
            .limit(limit)
        )
        return result.scalars().all()

    async def touch(self, request: DiscordAccessRequest) -> None:
        request.updated_at = utcnow()
        await self.session.flush()
