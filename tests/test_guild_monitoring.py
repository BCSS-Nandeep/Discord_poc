"""Guild-level monitoring master switch.

Independent of per-channel access: this governs whether monitors are permitted to run
in a guild at all, as an operator policy decision -- not a signal from Discord.
"""

from __future__ import annotations

import pytest

from app.core.enums import MonitorStatus, NotificationEvent
from app.core.exceptions import GuildMonitoringDisabledError, NotFoundError
from tests.fake_discord import GUILD_ID, PUBLIC_CHANNEL_ID, FakeChannel, FakeDiscord


async def _discovered_guild(services, fake_discord: FakeDiscord) -> None:
    """A guild only exists locally once discovered -- mirrors real usage."""

    await services.guilds.discover_guilds()


# ------------------------------------------------------------------- toggle basics --
async def test_new_guild_defaults_to_monitoring_enabled(services, fake_discord):
    await _discovered_guild(services, fake_discord)

    guild = await services.guilds.find_stored_guild(GUILD_ID)

    assert guild.monitoring_enabled is True


async def test_toggle_persists(services, fake_discord):
    await _discovered_guild(services, fake_discord)

    disabled = await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=False)
    assert disabled.monitoring_enabled is False

    reloaded = await services.guilds.find_stored_guild(GUILD_ID)
    assert reloaded.monitoring_enabled is False

    enabled = await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=True)
    assert enabled.monitoring_enabled is True


async def test_toggling_an_unknown_guild_raises(services):
    with pytest.raises(NotFoundError):
        await services.guilds.set_monitoring_enabled("999999999999999999", enabled=False)


async def test_rediscovery_does_not_reset_the_toggle(services, fake_discord):
    """A refresh from Discord must not silently re-enable a guild an operator disabled."""

    await _discovered_guild(services, fake_discord)
    await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=False)

    await services.guilds.discover_guilds()  # re-runs upsert()

    guild = await services.guilds.find_stored_guild(GUILD_ID)
    assert guild.monitoring_enabled is False


# --------------------------------------------------------------- disabling stops runs --
async def test_disabling_stops_every_running_monitor_in_the_guild(services, fake_discord):
    await _discovered_guild(services, fake_discord)
    monitor, _access, _scrape = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )
    assert monitor.status == MonitorStatus.RUNNING.value

    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=False)

    refreshed = await services.monitors.get_by_channel(PUBLIC_CHANNEL_ID)
    assert refreshed.status == MonitorStatus.STOPPED.value
    assert "disabled" in (refreshed.last_error or "").lower()


async def test_disabling_with_no_running_monitors_is_a_no_op_beyond_the_flag(
    services, fake_discord
):
    await _discovered_guild(services, fake_discord)

    guild = await services.workflow.set_guild_monitoring(GUILD_ID, enabled=False)

    assert guild.monitoring_enabled is False  # no exception, nothing to stop


async def test_disabling_emits_a_notification_naming_stopped_channels(
    services, fake_discord
):
    await _discovered_guild(services, fake_discord)
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)

    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=False)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.GUILD_MONITORING_DISABLED
    )
    assert len(rows) == 1
    assert "Stopped 1 monitor" in rows[0].message


async def test_enabling_emits_its_own_notification(services, fake_discord):
    await _discovered_guild(services, fake_discord)

    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=True)

    rows, _total = await services.notifications.list_notifications(
        event_type=NotificationEvent.GUILD_MONITORING_ENABLED
    )
    assert len(rows) == 1


async def test_stopping_multiple_monitors_in_one_guild(services, fake_discord):
    """Every RUNNING monitor in the guild stops, not just the first found."""

    await _discovered_guild(services, fake_discord)
    fake_discord.channels["200000000000000090"] = FakeChannel(
        channel_id="200000000000000090", name="second-channel", position=5
    )
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.workflow.start_monitor("200000000000000090", collect_history=False)

    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=False)

    for channel_id in (PUBLIC_CHANNEL_ID, "200000000000000090"):
        monitor = await services.monitors.get_by_channel(channel_id)
        assert monitor.status == MonitorStatus.STOPPED.value


# -------------------------------------------------------------- blocking new monitors --
async def test_start_monitor_is_refused_while_guild_monitoring_is_disabled(
    services, fake_discord
):
    await _discovered_guild(services, fake_discord)
    await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=False)

    with pytest.raises(GuildMonitoringDisabledError) as excinfo:
        await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)

    assert excinfo.value.details["guild_id"] == GUILD_ID
    assert await services.monitors.get_by_channel(PUBLIC_CHANNEL_ID) is None


async def test_start_monitor_works_again_after_re_enabling(services, fake_discord):
    await _discovered_guild(services, fake_discord)
    await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=False)
    await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=True)

    monitor, _access, _scrape = await services.workflow.start_monitor(
        PUBLIC_CHANNEL_ID, collect_history=False
    )

    assert monitor.status == MonitorStatus.RUNNING.value


async def test_re_enabling_does_not_auto_restart_stopped_monitors(services, fake_discord):
    """Resuming collection after a disable is an explicit decision, not automatic."""

    await _discovered_guild(services, fake_discord)
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=False)

    await services.workflow.set_guild_monitoring(GUILD_ID, enabled=True)

    monitor = await services.monitors.get_by_channel(PUBLIC_CHANNEL_ID)
    assert monitor.status == MonitorStatus.STOPPED.value


async def test_access_requests_are_unaffected_by_the_guild_toggle(services, fake_discord):
    """The master switch governs monitoring only, not the access-request workflow."""

    from tests.fake_discord import PRIVATE_CHANNEL_ID

    await _discovered_guild(services, fake_discord)
    await services.guilds.set_monitoring_enabled(GUILD_ID, enabled=False)

    outcome = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    assert outcome.created is True  # not blocked by the guild-level monitoring switch


# ---------------------------------------------------------------------------- HTTP ---
def test_monitoring_toggle_endpoint(app_client):
    app_client.get("/discord/guilds", params={"refresh": True})

    disabled = app_client.post(
        f"/discord/guilds/{GUILD_ID}/monitoring", json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["monitoring_enabled"] is False

    enabled = app_client.post(
        f"/discord/guilds/{GUILD_ID}/monitoring", json={"enabled": True}
    )
    assert enabled.status_code == 200
    assert enabled.json()["monitoring_enabled"] is True


def test_monitoring_toggle_for_unknown_guild_returns_404(app_client):
    response = app_client.post(
        "/discord/guilds/999999999999999999/monitoring", json={"enabled": False}
    )

    assert response.status_code == 404


def test_toggle_endpoint_stops_running_monitors_over_http(app_client):
    app_client.get("/discord/guilds", params={"refresh": True})
    app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/start",
        json={"collect_history": False},
    )

    app_client.post(f"/discord/guilds/{GUILD_ID}/monitoring", json={"enabled": False})

    status = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/status"
    ).json()
    assert status["monitor"]["status"] == "STOPPED"


def test_disabled_guild_blocks_monitor_start_over_http(app_client):
    app_client.get("/discord/guilds", params={"refresh": True})
    app_client.post(f"/discord/guilds/{GUILD_ID}/monitoring", json={"enabled": False})

    response = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/start", json={}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "GUILD_MONITORING_DISABLED"


def test_guild_response_includes_monitoring_enabled_field(app_client):
    app_client.get("/discord/guilds", params={"refresh": True})

    body = app_client.get(f"/discord/guilds/{GUILD_ID}").json()

    assert "monitoring_enabled" in body
    assert body["monitoring_enabled"] is True


def test_toggle_endpoint_is_documented(app_client):
    schema = app_client.get("/openapi.json").json()

    assert "/discord/guilds/{guild_id}/monitoring" in schema["paths"]
    assert "post" in schema["paths"]["/discord/guilds/{guild_id}/monitoring"]
