"""SQLAlchemy ORM models backing the Discord service SQLite database.

Discord snowflake ids are stored as ``TEXT``: they are 64-bit unsigned integers that do
not fit reliably in every integer column and are always strings on the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.enums import (
    AccessRequestStatus,
    ChannelAccessStatus,
    MonitorStatus,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used for every timestamp written by the service."""

    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """A datetime column that is always UTC-aware in Python.

    SQLite has no native timestamp type, so a ``DateTime`` column hands back naive
    datetimes even when it was written with a timezone.  Mixing those with the aware
    values this service produces raises ``can't compare offset-naive and offset-aware
    datetimes`` at exactly the wrong moment -- inside the reconciliation worker's due
    check.  Normalizing on both sides removes the whole class of bug: naive UTC on the
    way in (so SQLite's lexicographic comparisons stay correct) and UTC-aware on the
    way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for all Discord service tables."""


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class DiscordGuild(TimestampMixin, Base):
    """A Discord server (guild) the bot is a member of."""

    __tablename__ = "discord_guilds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    icon_url: Mapped[str | None] = mapped_column(String(500))
    owner_id: Mapped[str | None] = mapped_column(String(32))
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Master switch: when False, monitors in this guild are stopped and new ones
    # refuse to start. Independent of per-channel access -- this is an operator
    # policy toggle, not a permission signal from Discord.
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_discord_guilds_is_available", "is_available"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordGuild guild_id={self.guild_id} name={self.name!r}>"


class DiscordChannel(TimestampMixin, Base):
    """A channel plus the bot's most recently evaluated access to it."""

    __tablename__ = "discord_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    channel_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position: Mapped[int | None] = mapped_column(Integer)
    topic: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bot_can_view: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bot_can_read_history: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bot_can_read_message_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    access_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ChannelAccessStatus.UNKNOWN.value, index=True
    )
    access_reason: Mapped[str | None] = mapped_column(String(64))
    last_permission_check_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        Index("ix_discord_channels_guild_status", "guild_id", "access_status"),
        Index("ix_discord_channels_is_private", "is_private"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordChannel channel_id={self.channel_id} status={self.access_status}>"


class DiscordAccessRequest(TimestampMixin, Base):
    """An **application level** request to collect from a private channel.

    Discord has no API for a bot to ask for channel access, so this row simply records
    that we are waiting for a Discord server administrator to grant the bot permission.
    The reconciliation worker re-evaluates it on a fixed interval.
    """

    __tablename__ = "discord_access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    requested_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AccessRequestStatus.PENDING.value, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    denied_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    rejection_reason: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_by: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)

    # Follow-up actions to run once an administrator grants access.
    collect_history_on_grant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    monitor_on_grant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keyword_config_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_access_requests_status_next_check", "status", "next_check_at"),
        # At most one *open* request per channel. SQLite honours partial unique
        # indexes, which gives duplicate-prevention at the storage layer rather than
        # relying on an application-level check-then-insert race.
        Index(
            "ux_access_requests_open_channel",
            "channel_id",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<DiscordAccessRequest id={self.id} channel_id={self.channel_id} "
            f"status={self.status}>"
        )


class DiscordMessage(TimestampMixin, Base):
    """A collected message, normalized into the service's internal schema."""

    __tablename__ = "discord_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NOT NULL (with an empty-string fallback) so the unique constraint below always
    # applies -- SQLite treats NULLs as distinct inside a UNIQUE tuple.
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    author_id: Mapped[str | None] = mapped_column(String(32), index=True)
    author_name: Mapped[str | None] = mapped_column(String(200))
    author_is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    message_url: Mapped[str | None] = mapped_column(String(300))
    reply_to_message_id: Mapped[str | None] = mapped_column(String(32))
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attachments_json: Mapped[str | None] = mapped_column(Text)
    embeds_json: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    matched_keywords_json: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="rest")
    collected_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, index=True
    )

    __table_args__ = (
        # Duplicate protection. Declared as a named unique index rather than a table
        # UNIQUE constraint so the name survives into sqlite_master and can be
        # verified by operators (SQLite auto-names constraint indexes).
        Index(
            "ux_discord_messages_identity",
            "guild_id",
            "channel_id",
            "message_id",
            unique=True,
        ),
        Index("ix_discord_messages_channel_timestamp", "channel_id", "timestamp"),
        Index("ix_discord_messages_guild_timestamp", "guild_id", "timestamp"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordMessage message_id={self.message_id} channel={self.channel_id}>"


class DiscordMonitor(TimestampMixin, Base):
    """Live monitoring state for one channel. One monitor per channel."""

    __tablename__ = "discord_monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MonitorStatus.STOPPED.value, index=True
    )
    keyword_config_json: Mapped[str | None] = mapped_column(Text)
    store_all_messages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_event_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_message_id: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    messages_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_discord_monitors_status_guild", "status", "guild_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordMonitor channel_id={self.channel_id} status={self.status}>"


class DiscordUser(TimestampMixin, Base):
    """A person who has logged in with Discord.

    Only identity is stored. The OAuth2 access token is deliberately **not** persisted:
    the ``identify`` scope is used once at login to learn who the user is, after which
    the signed session cookie carries the login. Nothing later needs the token, so
    keeping it would be storing a credential for no purpose.
    """

    __tablename__ = "discord_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(200))
    global_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_discord_users_active", "is_active"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordUser discord_user_id={self.discord_user_id}>"


class DiscordNotification(Base):
    """An application-level status event. Never a Discord DM."""

    __tablename__ = "discord_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    access_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("discord_access_requests.id", ondelete="SET NULL"), index=True
    )
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    __table_args__ = (
        Index("ix_notifications_event_created", "event_type", "created_at"),
        Index("ix_notifications_channel_created", "channel_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DiscordNotification id={self.id} event={self.event_type}>"
