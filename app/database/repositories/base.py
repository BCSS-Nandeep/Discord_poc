"""Shared helpers for repositories.

Repositories are the only place that touches SQLAlchemy.  They never contain business
rules -- no permission logic, no state-machine decisions, no Discord calls.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Holds the session used by a repository instance."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def flush(self) -> None:
        await self.session.flush()
