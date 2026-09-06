"""(16) Monitor lifecycle, including the private-channel waiting path."""

from __future__ import annotations

import pytest

from app.core.enums import (
    MONITOR_TRANSITIONS,
    AccessRequestStatus,
    MonitorStatus,
    NotificationEvent,
    is_valid_transition,
)
from app.core.exceptions import InvalidStateTransitionError, NotFoundError
from app.discord.keywords import KeywordConfig
from tests.fake_discord import (
    GUILD_ID,
    MISSING_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
    make_message,
)


# ------------------------------------------------------------- accessible channel --
async def test_monitor_starts_running_for_an_accessible_channel(services):
    monitor, access, scrape = await services.workflow.start_monitor(PUBLIC_CHANNEL_ID)

    assert monitor.status == MonitorStatus.RUNNING.value
    assert monitor.started_at is not None
    assert access is None
    assert scrape is not None


async def test_monitor_start_collects_history_first(services):
    _monitor, _access, scrape = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=True
    )

    assert scrape.stored == 10
    assert await services.messages.count() == 10


async def test_monitor_start_can_skip_history(services):
    _monitor, _access, scrape = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )

    assert scrape is None
    assert await services.messages.count() == 0


async def test_monitor_start_emits_notification(services):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.MONITOR_STARTED
    )
    assert len(rows) == 1


async def test_monitor_start_is_idempotent(services):
    first, _a, _s = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )
    second, _a, _s = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )

    assert first.id == second.id
    assert second.status == MonitorStatus.RUNNING.value
    assert await services.monitors.count() == 1


async def test_keyword_config_is_stored_on_the_monitor(services):
    monitor, _a, _s = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID,
        keyword_config=KeywordConfig(keywords=["ransomware"]),
        collect_history=False,
    )

    config = services.monitors.keyword_config_for(monitor)

    assert config.keywords == ["ransomware"]


# ------------------------------------------------- private channel waiting path --
async def test_monitor_on_private_channel_waits_for_access(services):
    monitor, access, scrape = await services.workflow.start_monitor(PRIVATE_CHANNEL_ID)

    assert monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value
    assert scrape is None
    assert access is not None
    assert access.request.status == AccessRequestStatus.PENDING.value
    assert access.request.monitor_on_grant is True


async def test_waiting_monitor_becomes_running_after_access_is_granted(
    services, fake_discord: FakeDiscord
):
    """private -> WAITING_FOR_ACCESS -> permissions granted -> history -> RUNNING."""

    monitor, access, _scrape = await services.workflow.start_monitor(PRIVATE_CHANNEL_ID)
    assert monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value

    fake_discord.add_messages(PRIVATE_CHANNEL_ID, 5)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    outcome = await services.workflow.recheck_request(access.request.id)

    refreshed = await services.monitors.get_by_channel(PRIVATE_CHANNEL_ID)
    assert outcome.request_status is AccessRequestStatus.ACCEPTED
    assert refreshed.status == MonitorStatus.RUNNING.value
    assert outcome.scrape.stored == 5


async def test_monitor_start_reuses_an_existing_pending_request(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    _monitor, access, _scrape = await services.workflow.start_monitor(PRIVATE_CHANNEL_ID)

    assert access.request.id == created.request.id
    assert access.request.monitor_on_grant is True


async def test_monitor_for_unknown_channel_raises(services):
    with pytest.raises(NotFoundError):
        await services.workflow.start_monitor(MISSING_CHANNEL_ID)


# ------------------------------------------------------------------ stop / status --
async def test_monitor_stop_transitions_to_stopped(services):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)

    stopped = await services.workflow.stop_monitor(PUBLIC_CHANNEL_ID, reason="done")

    assert stopped.status == MonitorStatus.STOPPED.value
    assert stopped.stopped_at is not None


async def test_monitor_stop_emits_notification(services):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.workflow.stop_monitor(PUBLIC_CHANNEL_ID)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.MONITOR_STOPPED
    )
    assert len(rows) == 1


async def test_stopping_an_unknown_monitor_raises(services):
    with pytest.raises(NotFoundError):
        await services.workflow.stop_monitor(PUBLIC_CHANNEL_ID)


async def test_stopped_monitor_can_be_restarted(services):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.workflow.stop_monitor(PUBLIC_CHANNEL_ID)

    restarted, _a, _s = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )

    assert restarted.status == MonitorStatus.RUNNING.value


async def test_revoked_access_moves_a_running_monitor_back_to_waiting(services):
    monitor, _a, _s = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )

    await services.monitors.handle_access_revoked(monitor)

    assert monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value
    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.ACCESS_REVOKED
    )
    assert len(rows) == 1


async def test_running_channel_ids_feed_the_gateway(services):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.workflow.start_monitor(PRIVATE_CHANNEL_ID)

    running = await services.monitors.running_channel_ids()

    assert running == [PUBLIC_CHANNEL_ID]  # the waiting monitor is excluded


# ------------------------------------------------------------------ state machine --
def test_monitor_cannot_jump_from_stopped_to_running():
    assert not is_valid_transition(
        MONITOR_TRANSITIONS, MonitorStatus.STOPPED, MonitorStatus.RUNNING
    )


def test_waiting_for_access_cannot_go_straight_to_running():
    """Access must be confirmed via STARTING before a monitor runs."""

    assert not is_valid_transition(
        MONITOR_TRANSITIONS, MonitorStatus.WAITING_FOR_ACCESS, MonitorStatus.RUNNING
    )


async def test_pausing_a_stopped_monitor_is_rejected(services):
    monitor = await services.monitors.ensure_monitor(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID
    )

    with pytest.raises(InvalidStateTransitionError):
        await services.monitors.pause(monitor)


# ------------------------------------------------------------ live gateway path --
async def test_gateway_message_is_stored_and_counted(
    clients, database, services
):
    """The Gateway handler stores a live message and updates monitor counters."""

    await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID,
        keyword_config=KeywordConfig(keywords=["ransomware"]),
        collect_history=False,
    )
    await services.session.commit()

    payload = make_message(
        "888000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="ransomware alert"
    )
    await clients._handle_gateway_message(payload, PUBLIC_CHANNEL_ID, GUILD_ID)

    async with database.session() as session:
        graph = clients.build_services(session)
        monitor = await graph.monitors.get_by_channel(PUBLIC_CHANNEL_ID)
        stored = await graph.messages._repo.get_by_message_id("888000000000000001")
        matched_rows, _total = await graph.notifications.list_notifications(
            event_type=NotificationEvent.MESSAGE_MATCHED
        )

    assert stored is not None
    assert stored.source == "gateway"
    assert monitor.messages_seen == 1
    assert monitor.messages_matched == 1
    assert monitor.last_message_id == "888000000000000001"
    assert len(matched_rows) == 1


async def test_gateway_message_is_deduplicated_against_history(
    clients, database, services
):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=True)
    await services.session.commit()
    existing_id = "500000000000000005"

    payload = make_message(existing_id, channel_id=PUBLIC_CHANNEL_ID, content="dupe")
    await clients._handle_gateway_message(payload, PUBLIC_CHANNEL_ID, GUILD_ID)

    async with database.session() as session:
        graph = clients.build_services(session)
        total = await graph.messages.count()

    assert total == 10  # unchanged: the message was already collected


async def test_gateway_ignores_messages_for_unmonitored_channels(
    clients, database
):
    payload = make_message(
        "888000000000000002", channel_id=PUBLIC_CHANNEL_ID, content="ignored"
    )

    await clients._handle_gateway_message(payload, PUBLIC_CHANNEL_ID, GUILD_ID)

    async with database.session() as session:
        graph = clients.build_services(session)
        assert await graph.messages.count() == 0
