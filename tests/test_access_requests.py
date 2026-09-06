"""(8-12) The application-level private-channel access workflow.

Covers request creation, duplicate prevention, the 12-hour schedule, manual re-check,
the PENDING -> ACCEPTED transition and the guards that stop invalid transitions.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import (
    ACCESS_REQUEST_TRANSITIONS,
    AccessRequestStatus,
    ChannelAccessStatus,
    MonitorStatus,
    NotificationEvent,
    ensure_transition,
    is_valid_transition,
)
from app.core.exceptions import InvalidStateTransitionError, NotFoundError
from app.database.models import utcnow
from app.discord.keywords import KeywordConfig
from tests.fake_discord import (
    MISSING_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
    rate_limited,
)


# ------------------------------------------------------- creation (cases 8 and 9) --
async def test_private_channel_creates_pending_request(services):
    outcome = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    assert outcome.created is True
    assert outcome.channel_status is ChannelAccessStatus.PRIVATE
    assert outcome.request_status is AccessRequestStatus.PENDING
    assert "administrator" in outcome.message.lower()
    assert outcome.request.check_count == 0


async def test_pending_request_schedules_next_check_in_12_hours(services):
    outcome = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    request = outcome.request

    delta = request.next_check_at - request.requested_at
    assert delta == timedelta(hours=12)


async def test_recheck_hours_setting_drives_the_schedule(
    settings, database, fake_discord: FakeDiscord
):
    from app.discord.client import DiscordClientManager

    six_hourly = settings.model_copy(update={"access_recheck_hours": 6})
    manager = DiscordClientManager(
        six_hourly, database, rest_transport=fake_discord.transport()
    )
    await manager.start_rest()
    async with database.session() as session:
        graph = manager.build_services(session)
        outcome = await graph.workflow.request_access(PRIVATE_CHANNEL_ID)
        delta = outcome.request.next_check_at - outcome.request.requested_at
    await manager.close()

    assert delta == timedelta(hours=6)


async def test_accessible_channel_creates_no_pending_request(services):
    outcome = await services.workflow.request_access(PUBLIC_CHANNEL_ID)

    assert outcome.already_accessible is True
    assert outcome.created is False
    assert outcome.request is None
    assert outcome.channel_status is ChannelAccessStatus.PUBLIC_ACCESSIBLE


async def test_duplicate_access_request_is_not_created(services):
    first = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    second = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    assert first.created is True
    assert second.created is False
    assert second.request.id == first.request.id

    rows, total = await services.access_requests.list_requests(
        channel_id=PRIVATE_CHANNEL_ID
    )
    assert total == 1
    assert len(rows) == 1


async def test_duplicate_prevention_is_enforced_by_the_database(services):
    """The partial unique index is the real guard, not just an application check."""

    import sqlalchemy.exc

    await services.access_requests.create_pending(
        channel_id=PRIVATE_CHANNEL_ID, guild_id=None, channel_name="incident-response"
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await services.access_requests.create_pending(
            channel_id=PRIVATE_CHANNEL_ID,
            guild_id=None,
            channel_name="incident-response",
        )
    # The failed flush poisons the transaction; roll back so teardown can commit.
    await services.session.rollback()


async def test_unknown_channel_raises_not_found(services):
    with pytest.raises(NotFoundError):
        await services.workflow.request_access(MISSING_CHANNEL_ID)


async def test_request_creation_emits_notifications(services):
    await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    rows, _total = await services.notifications.list_notifications(
        channel_id=PRIVATE_CHANNEL_ID
    )
    events = {row.event_type for row in rows}

    assert NotificationEvent.ACCESS_REQUEST_CREATED.value in events
    assert NotificationEvent.ACCESS_PENDING.value in events


async def test_follow_up_actions_are_recorded_on_the_request(services):
    outcome = await services.workflow.request_access(
        PRIVATE_CHANNEL_ID,
        collect_history_on_grant=True,
        monitor_on_grant=True,
        keyword_config=KeywordConfig(keywords=["ransomware"]),
        requested_by="threat-intel",
        note="incident 2451",
    )
    request = outcome.request

    assert request.collect_history_on_grant is True
    assert request.monitor_on_grant is True
    assert request.requested_by == "threat-intel"
    assert "ransomware" in request.keyword_config_json


# ------------------------------------------------- manual recheck (cases 10, 12) --
async def test_recheck_keeps_request_pending_when_still_private(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.request_status is AccessRequestStatus.PENDING
    assert outcome.request.check_count == 1
    assert outcome.request.last_checked_at is not None


async def test_recheck_reschedules_next_check_12_hours_out(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    outcome = await services.workflow.recheck_request(created.request.id)
    request = outcome.request

    delta = request.next_check_at - request.last_checked_at
    assert timedelta(hours=11, minutes=59) < delta <= timedelta(hours=12)


async def test_recheck_transitions_pending_to_accepted_after_grant(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    assert created.request_status is AccessRequestStatus.PENDING

    # An administrator grants the bot access in Discord.
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.request_status is AccessRequestStatus.ACCEPTED
    assert outcome.request.accepted_at is not None
    assert outcome.request.next_check_at is None
    assert outcome.transitioned is True


async def test_acceptance_runs_historical_collection(
    services, fake_discord: FakeDiscord
):
    fake_discord.add_messages(PRIVATE_CHANNEL_ID, 15)
    created = await services.workflow.request_access(
        PRIVATE_CHANNEL_ID, collect_history_on_grant=True
    )
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.scrape is not None
    assert outcome.scrape.stored == 15


async def test_acceptance_starts_a_requested_monitor(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(
        PRIVATE_CHANNEL_ID, monitor_on_grant=True, collect_history_on_grant=False
    )
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.monitor is not None
    assert outcome.monitor.status == MonitorStatus.RUNNING.value


async def test_acceptance_emits_access_granted_notification(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    await services.workflow.recheck_request(created.request.id)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.ACCESS_GRANTED
    )
    assert len(rows) == 1
    assert rows[0].channel_id == PRIVATE_CHANNEL_ID


async def test_acceptance_updates_stored_channel_access_fields(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    await services.workflow.recheck_request(created.request.id)

    channel = await services.channels.get_stored_channel(PRIVATE_CHANNEL_ID)

    assert channel.bot_can_view is True
    assert channel.bot_can_read_history is True
    assert channel.access_status == ChannelAccessStatus.ACCESSIBLE.value


async def test_recheck_of_deleted_channel_is_denied_not_left_pending(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    del fake_discord.channels[PRIVATE_CHANNEL_ID]

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.request_status is AccessRequestStatus.DENIED
    assert outcome.request.denied_at is not None
    assert outcome.request.rejection_reason


async def test_transient_failure_leaves_request_pending(
    services, fake_discord: FakeDiscord
):
    """A Discord outage must never look like a permission decision."""

    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    for _ in range(6):
        fake_discord.script(f"/channels/{PRIVATE_CHANNEL_ID}", rate_limited(0.01))

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.request_status is AccessRequestStatus.PENDING
    assert outcome.request.last_error


async def test_transient_failure_emits_check_failed_notification(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    for _ in range(6):
        fake_discord.script(f"/channels/{PRIVATE_CHANNEL_ID}", rate_limited(0.01))

    await services.workflow.recheck_request(created.request.id)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.ACCESS_CHECK_FAILED
    )
    assert len(rows) == 1


async def test_recheck_of_missing_request_raises(services):
    with pytest.raises(NotFoundError):
        await services.workflow.recheck_request(9999)


# ----------------------------------------------------------- expiry and cancel --
async def test_expired_request_is_closed(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    created.request.expires_at = utcnow() - timedelta(days=1)
    await services.session.flush()

    outcome = await services.workflow.recheck_request(created.request.id)

    assert outcome.request_status is AccessRequestStatus.EXPIRED


async def test_cancel_stops_a_pending_request(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    cancelled = await services.workflow.cancel_request(created.request.id)

    assert cancelled.status == AccessRequestStatus.CANCELLED.value
    assert await services.access_requests.get_open_for_channel(PRIVATE_CHANNEL_ID) is None


async def test_cancel_stops_a_waiting_monitor(services):
    monitor, outcome, _scrape = await services.workflow.start_monitor(
        PRIVATE_CHANNEL_ID
    )
    assert monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value

    await services.workflow.cancel_request(outcome.request.id)
    refreshed = await services.monitors.get_by_channel(PRIVATE_CHANNEL_ID)

    assert refreshed.status == MonitorStatus.STOPPED.value


async def test_cancelled_request_allows_a_fresh_one(services):
    first = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    await services.workflow.cancel_request(first.request.id)

    second = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    assert second.created is True
    assert second.request.id != first.request.id


# ------------------------------------------------------------ state machine (12) --
def test_pending_may_transition_to_every_terminal_state():
    for target in (
        AccessRequestStatus.ACCEPTED,
        AccessRequestStatus.DENIED,
        AccessRequestStatus.ERROR,
        AccessRequestStatus.EXPIRED,
        AccessRequestStatus.CANCELLED,
    ):
        assert is_valid_transition(
            ACCESS_REQUEST_TRANSITIONS, AccessRequestStatus.PENDING, target
        )


def test_accepted_never_reverts_to_pending():
    """A transient Discord failure must not undo a granted access request."""

    assert not is_valid_transition(
        ACCESS_REQUEST_TRANSITIONS,
        AccessRequestStatus.ACCEPTED,
        AccessRequestStatus.PENDING,
    )
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(
            ACCESS_REQUEST_TRANSITIONS,
            AccessRequestStatus.ACCEPTED,
            AccessRequestStatus.PENDING,
        )


async def test_recheck_of_accepted_request_is_a_no_op(
    services, fake_discord: FakeDiscord
):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    await services.workflow.recheck_request(created.request.id)

    again = await services.workflow.recheck_request(created.request.id)

    assert again.request_status is AccessRequestStatus.ACCEPTED
    assert "no re-check performed" in again.message


async def test_concurrent_checks_are_serialized_by_the_claim(services):
    """The conditional UPDATE is what prevents duplicate processing."""

    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    now = utcnow()

    first = await services.access_requests._repo.claim_for_check(
        request_id=created.request.id,
        now=now + timedelta(hours=13),
        next_check_at=now + timedelta(hours=25),
    )
    second = await services.access_requests._repo.claim_for_check(
        request_id=created.request.id,
        now=now + timedelta(hours=13),
        next_check_at=now + timedelta(hours=25),
    )

    assert first is True
    assert second is False  # already claimed and rescheduled
