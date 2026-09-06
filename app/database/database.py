"""Async SQLAlchemy engine / session management for SQLite.

SQLite needs a little tuning before it behaves well under a web server plus a
background worker:

* **WAL journal** so the reconciliation worker can write while HTTP requests read.
* **busy_timeout** so a concurrent writer waits instead of raising ``database is locked``.
* **foreign_keys ON**, which SQLite disables by default.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models import Base

logger = get_logger(__name__)


class Database:
    """Owns the async engine and session factory for the service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    # ------------------------------------------------------------------ lifecycle --
    def connect(self) -> AsyncEngine:
        """Create the engine (idempotent)."""

        if self._engine is not None:
            return self._engine

        kwargs: dict[str, Any] = {
            "echo": self._settings.database_echo,
            "future": True,
        }
        if self._settings.is_in_memory_database:
            # One shared connection, otherwise every session gets an empty database.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}

        engine = create_async_engine(self._settings.database_url, **kwargs)
        self._configure_sqlite_pragmas(engine)

        self._engine = engine
        self._session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        logger.info(
            "Database engine created",
            extra={"driver": engine.url.drivername, "database": engine.url.database},
        )
        return engine

    def _configure_sqlite_pragmas(self, engine: AsyncEngine) -> None:
        busy_timeout = self._settings.database_busy_timeout_ms
        in_memory = self._settings.is_in_memory_database

        @event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - hook
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
                if not in_memory:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    async def create_all(self) -> None:
        """Create every table and index if it does not exist yet.

        This standalone service owns its own SQLite file, so ``create_all`` is a safe
        and sufficient substitute for a migration tool.
        """

        engine = self.connect()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info(
            "Database schema ready", extra={"tables": len(Base.metadata.tables)}
        )

    async def healthcheck(self) -> bool:
        """Return ``True`` when a trivial query succeeds."""

        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - health must never raise
            logger.warning("Database health check failed", extra={"error": str(exc)})
            return False

    async def disconnect(self) -> None:
        """Dispose the engine and close pooled connections."""

        if self._engine is not None:
            await self._engine.dispose()
            logger.info("Database engine disposed")
        self._engine = None
        self._session_factory = None

    # -------------------------------------------------------------------- sessions --
    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self.connect()
        assert self._session_factory is not None
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session context manager that commits on success and rolls back on error."""

        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
