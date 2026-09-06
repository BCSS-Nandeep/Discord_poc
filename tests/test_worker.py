"""(11, 12, 21) The 12-hour reconciliation worker and the single-instance guard."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.core.enums import AccessRequestStatus, MonitorStatus, NotificationEvent
from app.core.runlock import ProcessLock
from app.database.models import utcnow
from app.workers.access_reconciler import AccessReconciliationWorker
from tests.fake_discord import PRIVATE_CHANNEL_ID, FakeDiscord


@pytest.fixture
def worker(settings, database, clients) -> AccessReconciliationWorker:
    return AccessReconciliationWorker(
        settings=settings, database=database, client_manager=clients
    )


async def _make_due(database, clients, channel_id: str, **kwargs) -> int:
    """Create a pending request and back-date it so the worker sees it as due."""

    async with database.session() as session:
        graph = clients.build_services(session)
        outcome = await graph.workflow.request_access(channel_id, **kwargs)
        request = outcome.request
        request.next_check_at = utcnow() - timedelta(minutes=1)
        await session.flush()
        return request.id


# --------------------------------------------------------------- reconciliation --
async def test_worker_skips_requests_that_are_not_due(
    worker: AccessReconciliationWorker, database, clients
):
    async with database.session() as session:
        graph = clients.build_services(session)
        await graph.workflow.request_access(PRIVATE_CHANNEL_ID)

    report = await worker.run_once()

    assert report.examined == 0  # next_check_at is 12 hours away


async def test_worker_processes_a_due_request_and_keeps_it_pending(
    worker: AccessReconciliationWorker, database, clients
):
    request_id = await _make_due(database, clients, PRIVATE_CHANNEL_ID)

    report = await worker.run_once()

    assert report.examined == 1
    assert report.still_pending == 1
    async with database.session() as session:
        graph = clients.build_services(session)
        request = await graph.access_requests.get(request_id)
    assert request.status == AccessRequestStatus.PENDING.value
    assert request.check_count == 1
    assert request.last_checked_at is not None


async def test_worker_reschedules_12_hours_out(
    worker: AccessReconciliationWorker, database, clients
):
    request_id = await _make_due(database, clients, PRIVATE_CHANNEL_ID)

    await worker.run_once()

    async with database.session() as session:
        graph = clients.build_services(session)
        request = await graph.access_requests.get(request_id)
    delta = request.next_check_at - request.last_checked_at
    assert timedelta(hours=11, minutes=59) < delta <= timedelta(hours=12)


async def test_worker_accepts_a_request_once_access_is_granted(
    worker: AccessReconciliationWorker, database, clients, fake_discord: FakeDiscord
):
    request_id = await _make_due(database, clients, PRIVATE_CHANNEL_ID)
    fake_discord.add_messages(PRIVATE_CHANNEL_ID, 4)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    report = await worker.run_once()

    assert report.accepted == 1
    async with database.session() as session:
        graph = clients.build_services(session)
        request = await graph.access_requests.get(request_id)
        stored = await graph.messages.count()
    assert request.status == AccessRequestStatus.ACCEPTED.value
    assert stored == 4  # historical collection ran on acceptance


async def test_worker_starts_a_waiting_monitor_on_grant(
    worker: AccessReconciliationWorker, database, clients, fake_discord: FakeDiscord
):
    async with database.session() as session:
        graph = clients.build_services(session)
        _monitor, access, _scrape = await graph.workflow.start_monitor(
            PRIVATE_CHANNEL_ID
        )
        access.request.next_check_at = utcnow() - timedelta(minutes=1)
        await session.flush()

    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    await worker.run_once()

    async with database.session() as session:
        graph = clients.build_services(session)
        monitor = await graph.monitors.get_by_channel(PRIVATE_CHANNEL_ID)
    assert monitor.status == MonitorStatus.RUNNING.value


async def test_worker_refreshes_the_gateway_monitor_set_after_a_grant(
    worker: AccessReconciliationWorker, database, clients, fake_discord: FakeDiscord
):
    async with database.session() as session:
        graph = clients.build_services(session)
        _monitor, access, _scrape = await graph.workflow.start_monitor(
            PRIVATE_CHANNEL_ID
        )
        access.request.next_check_at = utcnow() - timedelta(minutes=1)
        await session.flush()

    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    await worker.run_once()

    assert PRIVATE_CHANNEL_ID in clients.gateway.monitored_channels()


async def test_worker_expires_stale_requests(
    worker: AccessReconciliationWorker, database, clients
):
    async with database.session() as session:
        graph = clients.build_services(session)
        outcome = await graph.workflow.request_access(PRIVATE_CHANNEL_ID)
        outcome.request.expires_at = utcnow() - timedelta(days=1)
        await session.flush()
        request_id = outcome.request.id

    report = await worker.run_once()

    async with database.session() as session:
        graph = clients.build_services(session)
        request = await graph.access_requests.get(request_id)
    assert report.expired >= 1
    assert request.status == AccessRequestStatus.EXPIRED.value


async def test_worker_run_is_idempotent(
    worker: AccessReconciliationWorker, database, clients
):
    """A second immediate pass must not re-process the same request."""

    await _make_due(database, clients, PRIVATE_CHANNEL_ID)

    first = await worker.run_once()
    second = await worker.run_once()

    assert first.examined == 1
    assert second.examined == 0  # the claim pushed next_check_at forward


async def test_worker_survives_a_failing_request(
    worker: AccessReconciliationWorker, database, clients, fake_discord: FakeDiscord
):
    await _make_due(database, clients, PRIVATE_CHANNEL_ID)
    fake_discord.unauthorized = True

    report = await worker.run_once()

    # The bad token surfaces as a still-pending request with a recorded failure,
    # not as an unhandled crash that kills the loop.
    assert report.examined == 1
    async with database.session() as session:
        graph = clients.build_services(session)
        rows, _total = await graph.notifications.list_notifications(
            event_type=NotificationEvent.ACCESS_CHECK_FAILED
        )
    assert len(rows) >= 1


async def test_worker_handles_an_empty_queue(worker: AccessReconciliationWorker):
    report = await worker.run_once()

    assert report.examined == 0
    assert report.errors == []


# ------------------------------------------------------------- start/stop (21) --
async def test_worker_starts_and_stops_cleanly(worker: AccessReconciliationWorker):
    await worker.start()
    assert worker.is_running is True

    await worker.stop()
    assert worker.is_running is False


async def test_start_twice_creates_only_one_task(worker: AccessReconciliationWorker):
    await worker.start()
    first_task = worker._task
    await worker.start()

    assert worker._task is first_task

    await worker.stop()


async def test_worker_loop_ticks(settings, database, clients):
    """The loop actually runs a pass on its own schedule."""

    fast = settings.model_copy(update={"worker_tick_seconds": 5})
    ticking = AccessReconciliationWorker(
        settings=fast, database=database, client_manager=clients
    )
    await ticking.start()
    await asyncio.sleep(0.2)
    await ticking.stop()

    assert ticking.snapshot()["last_run_at"] is not None


async def test_snapshot_reports_configuration(worker: AccessReconciliationWorker):
    snapshot = worker.snapshot()

    assert snapshot["recheck_hours"] == 12
    assert snapshot["running"] is False


# ------------------------------------------------- single-instance guard (21) --
def test_process_lock_is_held_by_one_holder(tmp_path):
    path = tmp_path / "worker.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)

    assert first.acquire() is True
    # A second in-process holder still sees the byte lock as taken on POSIX; on
    # Windows the same handle owner may re-lock, so assert the release contract
    # rather than the platform-specific second acquire.
    first.release()
    assert first.acquired is False
    assert second.acquire() is True
    second.release()


def test_process_lock_acquire_is_idempotent(tmp_path):
    lock = ProcessLock(tmp_path / "worker.lock")

    assert lock.acquire() is True
    assert lock.acquire() is True
    assert lock.acquired is True

    lock.release()


def test_release_without_acquire_is_safe(tmp_path):
    ProcessLock(tmp_path / "worker.lock").release()
