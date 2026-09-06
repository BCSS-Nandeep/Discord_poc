"""The periodic access-request reconciliation worker.

Every ``WORKER_TICK_SECONDS`` the worker looks for PENDING access requests whose
``next_check_at`` has come due (12 hours after the previous check by default) and
re-evaluates whether a Discord administrator has granted the bot access yet.

Idempotency and concurrency
---------------------------
Each request is claimed with a conditional ``UPDATE`` before any Discord call (see
:meth:`AccessRequestRepository.claim_for_check`).  The claim also pushes
``next_check_at`` forward, so:

* two workers, or a worker racing a manual re-check, cannot process the same request;
* a crash mid-check cannot produce a hot retry loop;
* re-running a tick is harmless.

A single OS-level lock (:class:`app.core.runlock.ProcessLock`) additionally ensures only
one process runs this worker at all, which matters under ``uvicorn --reload`` and
``--workers N``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.core.enums import AccessRequestStatus, NotificationEvent
from app.core.logging import get_logger
from app.database.database import Database
from app.database.models import utcnow

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from app.discord.client import DiscordClientManager

logger = get_logger(__name__)


@dataclass(slots=True)
class ReconciliationReport:
    """Outcome of one reconciliation pass."""

    examined: int = 0
    claimed: int = 0
    accepted: int = 0
    still_pending: int = 0
    expired: int = 0
    denied: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "examined": self.examined,
            "claimed": self.claimed,
            "accepted": self.accepted,
            "still_pending": self.still_pending,
            "expired": self.expired,
            "denied": self.denied,
            "failed": self.failed,
        }


class AccessReconciliationWorker:
    """Background task that reconciles PENDING access requests on a schedule."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        client_manager: DiscordClientManager,
    ) -> None:
        self._settings = settings
        self._database = database
        self._clients = client_manager
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self._last_report: ReconciliationReport | None = None
        self._last_run_at: datetime | None = None

    # ------------------------------------------------------------------ lifecycle --
    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the worker loop (idempotent -- a second call is a no-op)."""

        if self.is_running:
            logger.debug("Reconciliation worker already running; start() ignored")
            return
        self._stop_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="access-reconciler")
        logger.info(
            "Access reconciliation worker started",
            extra={
                "tick_seconds": self._settings.worker_tick_seconds,
                "recheck_hours": self._settings.access_recheck_hours,
            },
        )

    async def stop(self) -> None:
        """Signal the loop to finish and wait for it."""

        if self._task is None:
            self._running = False
            return
        self._stop_event.set()
        self._task.cancel()
        # Shutdown is best effort: a failing tick must not block the app closing.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
        self._running = False
        logger.info("Access reconciliation worker stopped")

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                    logger.exception("Reconciliation tick failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.worker_tick_seconds,
                    )
                except TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            logger.debug("Reconciliation loop cancelled")
            raise

    # ---------------------------------------------------------------- one full pass --
    async def run_once(self) -> ReconciliationReport:
        """Run a single reconciliation pass. Safe to call directly (tests, manual)."""

        report = ReconciliationReport()
        now = utcnow()

        await self._expire_stale_requests(report, now=now)

        async with self._database.session() as session:
            services = self._clients.build_services(session)
            due = await services.access_requests.list_due(
                now=now, limit=self._settings.worker_batch_size
            )
            due_ids = [(request.id, request.channel_id) for request in due]

        report.examined = len(due_ids)
        if not due_ids:
            self._last_report = report
            self._last_run_at = now
            logger.debug("Reconciliation tick found no due access requests")
            return report

        logger.info(
            "Reconciling due access requests", extra={"due_count": len(due_ids)}
        )

        granted_any = False
        for request_id, channel_id in due_ids:
            try:
                outcome = await self._process_one(request_id)
            except Exception as exc:  # noqa: BLE001 - keep going through the batch
                report.failed += 1
                report.errors.append(f"request {request_id}: {type(exc).__name__}")
                logger.exception(
                    "Failed to reconcile access request",
                    extra={"request_id": request_id, "channel_id": channel_id},
                )
                await self._emit_check_failure(request_id, channel_id, exc)
                continue

            if outcome is None:
                continue

            status = outcome.request.status if outcome.request else None
            if status == AccessRequestStatus.ACCEPTED.value:
                report.accepted += 1
                granted_any = True
            elif status == AccessRequestStatus.EXPIRED.value:
                report.expired += 1
            elif status == AccessRequestStatus.DENIED.value:
                report.denied += 1
            elif status == AccessRequestStatus.PENDING.value:
                report.still_pending += 1
            report.claimed += 1

        if granted_any:
            # Newly RUNNING monitors must reach the Gateway's monitored set.
            await self._clients.refresh_monitored_channels()

        self._last_report = report
        self._last_run_at = utcnow()
        logger.info("Reconciliation tick finished", extra=report.as_dict())
        return report

    async def _process_one(self, request_id: int):
        """Re-check one request in its own transaction."""

        async with self._database.session() as session:
            services = self._clients.build_services(session)
            # ``force=False`` uses the due-aware claim, which is the concurrency guard.
            return await services.workflow.recheck_request(request_id, force=False)

    async def _expire_stale_requests(
        self, report: ReconciliationReport, *, now: datetime
    ) -> None:
        """Close PENDING requests that outlived ACCESS_REQUEST_EXPIRY_DAYS."""

        async with self._database.session() as session:
            services = self._clients.build_services(session)
            stale = await services.access_requests.list_expired_candidates(now=now)
            for request in stale:
                try:
                    await services.access_requests.mark_expired(request)
                    report.expired += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to expire access request",
                        extra={"request_id": request.id},
                    )

    async def _emit_check_failure(
        self, request_id: int, channel_id: str, exc: Exception
    ) -> None:
        """Record an ACCESS_CHECK_FAILED notification for an unexpected error."""

        try:
            async with self._database.session() as session:
                services = self._clients.build_services(session)
                await services.notifications.emit(
                    NotificationEvent.ACCESS_CHECK_FAILED,
                    f"Access check for channel {channel_id} failed unexpectedly.",
                    access_request_id=request_id,
                    channel_id=channel_id,
                    payload={"error_type": type(exc).__name__},
                )
        except Exception:  # noqa: BLE001 - notification failure must not cascade
            logger.exception("Could not record ACCESS_CHECK_FAILED notification")

    # --------------------------------------------------------------------- status --
    def snapshot(self) -> dict[str, object]:
        """Status summary for the health endpoint."""

        return {
            "enabled": self._settings.enable_background_workers,
            "running": self.is_running,
            "tick_seconds": self._settings.worker_tick_seconds,
            "recheck_hours": self._settings.access_recheck_hours,
            "last_run_at": self._last_run_at,
            "last_report": self._last_report.as_dict() if self._last_report else None,
        }
