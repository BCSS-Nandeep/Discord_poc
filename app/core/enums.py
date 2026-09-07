"""Explicit state machines for the Discord service.

Every status stored in SQLite is one of these string enum values.  Transitions are
validated through :func:`ensure_transition` so state cannot silently move backwards
(for example ``ACCEPTED -> PENDING`` because of a transient Discord outage).
"""

from __future__ import annotations

from enum import StrEnum


class ChannelAccessStatus(StrEnum):
    """Effective access the bot has to a channel."""

    UNKNOWN = "UNKNOWN"
    PUBLIC_ACCESSIBLE = "PUBLIC_ACCESSIBLE"
    PRIVATE = "PRIVATE"
    ACCESSIBLE = "ACCESSIBLE"
    ERROR = "ERROR"


class AccessRequestStatus(StrEnum):
    """Lifecycle of an *application level* private-channel access request.

    ``PRIVATE`` is accepted for compatibility with callers that describe a channel's
    visibility, but the workflow itself always uses ``PENDING`` while waiting for a
    Discord server administrator to grant the bot permission.
    """

    PRIVATE = "PRIVATE"
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DENIED = "DENIED"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class MonitorStatus(StrEnum):
    """Lifecycle of a live channel monitor."""

    WAITING_FOR_ACCESS = "WAITING_FOR_ACCESS"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class NotificationEvent(StrEnum):
    """Application-level notification events (never Discord DMs)."""

    ACCESS_REQUEST_CREATED = "ACCESS_REQUEST_CREATED"
    ACCESS_PENDING = "ACCESS_PENDING"
    ACCESS_GRANTED = "ACCESS_GRANTED"
    ACCESS_DENIED = "ACCESS_DENIED"
    ACCESS_CHECK_FAILED = "ACCESS_CHECK_FAILED"
    ACCESS_REVOKED = "ACCESS_REVOKED"
    MONITOR_STARTED = "MONITOR_STARTED"
    MONITOR_STOPPED = "MONITOR_STOPPED"
    GUILD_MONITORING_ENABLED = "GUILD_MONITORING_ENABLED"
    GUILD_MONITORING_DISABLED = "GUILD_MONITORING_DISABLED"
    MESSAGE_MATCHED = "MESSAGE_MATCHED"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class AccessReason(StrEnum):
    """Machine-readable explanation produced by the PermissionService."""

    FULLY_ACCESSIBLE = "FULLY_ACCESSIBLE"
    CHANNEL_NOT_FOUND = "CHANNEL_NOT_FOUND"
    BOT_CANNOT_VIEW_CHANNEL = "BOT_CANNOT_VIEW_CHANNEL"
    MISSING_READ_MESSAGE_HISTORY = "MISSING_READ_MESSAGE_HISTORY"
    MESSAGE_CONTENT_NOT_ENABLED = "MESSAGE_CONTENT_NOT_ENABLED"
    NOT_A_TEXT_CHANNEL = "NOT_A_TEXT_CHANNEL"
    BOT_NOT_IN_GUILD = "BOT_NOT_IN_GUILD"
    INVALID_BOT_TOKEN = "INVALID_BOT_TOKEN"
    RATE_LIMITED = "RATE_LIMITED"
    DISCORD_ERROR = "DISCORD_ERROR"
    UNKNOWN = "UNKNOWN"


#: Reasons that describe a *transient* condition.  A transient failure must never
#: downgrade a previously ACCEPTED request; the worker simply retries later.
TRANSIENT_REASONS: frozenset[AccessReason] = frozenset(
    {AccessReason.RATE_LIMITED, AccessReason.DISCORD_ERROR}
)


ACCESS_REQUEST_TRANSITIONS: dict[AccessRequestStatus, frozenset[AccessRequestStatus]] = {
    AccessRequestStatus.PRIVATE: frozenset(
        {AccessRequestStatus.PENDING, AccessRequestStatus.CANCELLED}
    ),
    AccessRequestStatus.PENDING: frozenset(
        {
            AccessRequestStatus.ACCEPTED,
            AccessRequestStatus.DENIED,
            AccessRequestStatus.ERROR,
            AccessRequestStatus.EXPIRED,
            AccessRequestStatus.CANCELLED,
            AccessRequestStatus.PENDING,  # idempotent re-check keeps it pending
        }
    ),
    # A transient Discord failure must not push an accepted request back to PENDING.
    AccessRequestStatus.ACCEPTED: frozenset({AccessRequestStatus.CANCELLED}),
    AccessRequestStatus.DENIED: frozenset({AccessRequestStatus.PENDING}),
    AccessRequestStatus.ERROR: frozenset(
        {AccessRequestStatus.PENDING, AccessRequestStatus.CANCELLED}
    ),
    AccessRequestStatus.EXPIRED: frozenset({AccessRequestStatus.PENDING}),
    AccessRequestStatus.CANCELLED: frozenset({AccessRequestStatus.PENDING}),
}

MONITOR_TRANSITIONS: dict[MonitorStatus, frozenset[MonitorStatus]] = {
    MonitorStatus.WAITING_FOR_ACCESS: frozenset(
        {MonitorStatus.STARTING, MonitorStatus.STOPPED, MonitorStatus.ERROR}
    ),
    MonitorStatus.STARTING: frozenset(
        {MonitorStatus.RUNNING, MonitorStatus.ERROR, MonitorStatus.STOPPED}
    ),
    MonitorStatus.RUNNING: frozenset(
        {
            MonitorStatus.PAUSED,
            MonitorStatus.STOPPED,
            MonitorStatus.ERROR,
            MonitorStatus.WAITING_FOR_ACCESS,  # access was revoked by an admin
        }
    ),
    MonitorStatus.PAUSED: frozenset(
        {MonitorStatus.RUNNING, MonitorStatus.STOPPED, MonitorStatus.ERROR}
    ),
    MonitorStatus.STOPPED: frozenset(
        {MonitorStatus.STARTING, MonitorStatus.WAITING_FOR_ACCESS}
    ),
    MonitorStatus.ERROR: frozenset(
        {MonitorStatus.STARTING, MonitorStatus.WAITING_FOR_ACCESS, MonitorStatus.STOPPED}
    ),
}


def is_valid_transition(
    transitions: dict, current: StrEnum, target: StrEnum
) -> bool:
    """Return ``True`` when ``current -> target`` is an allowed transition."""

    if current == target:
        return target in transitions.get(current, frozenset())
    return target in transitions.get(current, frozenset())


def ensure_transition(transitions: dict, current: StrEnum, target: StrEnum) -> None:
    """Raise :class:`InvalidStateTransitionError` for a disallowed transition."""

    from app.core.exceptions import InvalidStateTransitionError

    if not is_valid_transition(transitions, current, target):
        raise InvalidStateTransitionError(
            f"Invalid transition {current.value} -> {target.value}"
        )
