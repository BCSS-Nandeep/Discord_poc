"""An in-memory stand-in for the Discord REST API.

Every unit and integration test runs against this, so the suite never needs a real
Discord server, a real bot token, or network access.  Permission behaviour mirrors
Discord's: hidden channels answer 403 on ``GET /channels/{id}`` and channels without
*Read Message History* answer 403 on ``GET /channels/{id}/messages``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

BOT_ID = "900000000000000001"
GUILD_ID = "100000000000000001"
PUBLIC_CHANNEL_ID = "200000000000000001"
PRIVATE_CHANNEL_ID = "200000000000000002"
NO_HISTORY_CHANNEL_ID = "200000000000000003"
MISSING_CHANNEL_ID = "200000000000000009"

VIEW_CHANNEL = 1 << 10


@dataclass
class FakeChannel:
    """A channel in the fake guild."""

    channel_id: str
    name: str
    channel_type: int = 0
    guild_id: str = GUILD_ID
    parent_id: str | None = None
    position: int = 0
    #: False -> ``GET /channels/{id}`` answers 403 (bot cannot see it).
    bot_can_view: bool = True
    #: False -> ``GET /channels/{id}/messages`` answers 403.
    bot_can_read_history: bool = True
    #: True -> @everyone is denied VIEW_CHANNEL, i.e. Discord calls it private.
    everyone_denied: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        overwrites = []
        if self.everyone_denied:
            overwrites.append(
                {"id": self.guild_id, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}
            )
        return {
            "id": self.channel_id,
            "type": self.channel_type,
            "guild_id": self.guild_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "position": self.position,
            "topic": None,
            "permission_overwrites": overwrites,
        }


def make_message(
    message_id: str,
    *,
    channel_id: str,
    guild_id: str = GUILD_ID,
    content: str = "hello",
    author_id: str = "800000000000000001",
    author_name: str = "analyst",
    created: datetime | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Discord message object as the REST API would return it."""

    moment = created or datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return {
        "id": message_id,
        "type": 0,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "author": {
            "id": author_id,
            "username": author_name,
            "global_name": author_name.title(),
            "bot": False,
        },
        "content": content,
        "timestamp": moment.isoformat(),
        "edited_timestamp": None,
        "attachments": attachments or [],
        "embeds": [],
        "pinned": False,
        "tts": False,
        "mention_everyone": False,
    }


class FakeDiscord:
    """Routable fake Discord API with configurable failures."""

    def __init__(self) -> None:
        self.channels: dict[str, FakeChannel] = {}
        self.guilds: dict[str, dict[str, Any]] = {
            GUILD_ID: {
                "id": GUILD_ID,
                "name": "Threat Intel Server",
                "icon": "abc123",
                "owner_id": "700000000000000001",
            }
        }
        self.bot = {
            "id": BOT_ID,
            "username": "collector-bot",
            "global_name": "Collector",
            "discriminator": "0",
            "bot": True,
            "avatar": None,
        }
        #: Requests recorded for assertions: ``(method, path, query)``.
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        #: Queue of canned responses keyed by path suffix, popped per call.
        self.scripted: dict[str, list[httpx.Response]] = {}
        #: Set to reject every call with 401.
        self.unauthorized = False
        #: ``{channel_id: n}`` -> history calls after the n-th answer 403, which
        #: simulates an administrator revoking access midway through a scrape.
        self.deny_history_after: dict[str, int] = {}
        self._history_calls: dict[str, int] = {}
        self.seed()

    # ------------------------------------------------------------------ seeding --
    def seed(self) -> None:
        base = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        public = FakeChannel(
            channel_id=PUBLIC_CHANNEL_ID, name="general", position=1
        )
        public.messages = [
            make_message(
                str(500000000000000000 + index),
                channel_id=PUBLIC_CHANNEL_ID,
                content=f"message {index}",
                created=base + timedelta(minutes=index),
            )
            for index in range(10)
        ]
        self.channels[PUBLIC_CHANNEL_ID] = public

        self.channels[PRIVATE_CHANNEL_ID] = FakeChannel(
            channel_id=PRIVATE_CHANNEL_ID,
            name="incident-response",
            position=2,
            bot_can_view=False,
            bot_can_read_history=False,
            everyone_denied=True,
        )
        self.channels[NO_HISTORY_CHANNEL_ID] = FakeChannel(
            channel_id=NO_HISTORY_CHANNEL_ID,
            name="announcements",
            position=3,
            bot_can_view=True,
            bot_can_read_history=False,
        )

    def grant_access(self, channel_id: str) -> None:
        """Simulate an administrator granting the bot access to a channel."""

        channel = self.channels[channel_id]
        channel.bot_can_view = True
        channel.bot_can_read_history = True

    def revoke_access(self, channel_id: str) -> None:
        channel = self.channels[channel_id]
        channel.bot_can_view = False
        channel.bot_can_read_history = False

    def add_messages(self, channel_id: str, count: int, *, prefix: str = "msg") -> None:
        channel = self.channels[channel_id]
        base = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        start = 600000000000000000 + len(channel.messages)
        for index in range(count):
            channel.messages.append(
                make_message(
                    str(start + index),
                    channel_id=channel_id,
                    content=f"{prefix} {index}",
                    created=base + timedelta(minutes=index),
                )
            )

    def script(self, path_suffix: str, response: httpx.Response) -> None:
        """Queue a canned response for the next call whose path ends with the suffix."""

        self.scripted.setdefault(path_suffix, []).append(response)

    # ------------------------------------------------------------------ routing --
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.calls.append((request.method, path, query))

        for suffix, queue in self.scripted.items():
            if path.endswith(suffix) and queue:
                return queue.pop(0)

        if self.unauthorized:
            return _json(401, {"message": "401: Unauthorized", "code": 0})

        if not request.headers.get("Authorization", "").startswith("Bot "):
            return _json(401, {"message": "401: Unauthorized", "code": 0})

        if path.endswith("/users/@me"):
            return _json(200, self.bot)
        if path.endswith("/users/@me/guilds"):
            return _json(200, list(self.guilds.values()))
        if path.endswith("/applications/@me"):
            return _json(200, {"id": BOT_ID, "flags": 0})

        if "/guilds/" in path:
            guild_id = path.split("/guilds/")[1].split("/")[0]
            if guild_id not in self.guilds:
                return _json(404, {"message": "Unknown Guild", "code": 10004})
            if path.endswith("/channels"):
                return _json(
                    200,
                    [
                        channel.to_payload()
                        for channel in self.channels.values()
                        if channel.guild_id == guild_id
                    ],
                )
            if path.endswith("/roles"):
                return _json(
                    200,
                    [{"id": guild_id, "name": "@everyone", "permissions": "0"}],
                )
            if "/members/" in path:
                return _json(200, {"user": self.bot, "roles": []})
            return _json(200, self.guilds[guild_id])

        if "/channels/" in path:
            channel_id = path.split("/channels/")[1].split("/")[0]
            channel = self.channels.get(channel_id)
            if channel is None:
                return _json(404, {"message": "Unknown Channel", "code": 10003})

            if path.endswith("/messages"):
                seen = self._history_calls.get(channel_id, 0) + 1
                self._history_calls[channel_id] = seen
                limit_after = self.deny_history_after.get(channel_id)
                if limit_after is not None and seen > limit_after:
                    return _json(403, {"message": "Missing Access", "code": 50001})
                if not channel.bot_can_view or not channel.bot_can_read_history:
                    return _json(
                        403, {"message": "Missing Access", "code": 50001}
                    )
                return _json(200, _paginate(channel.messages, query))

            if not channel.bot_can_view:
                return _json(403, {"message": "Missing Access", "code": 50001})
            return _json(200, channel.to_payload())

        return _json(404, {"message": "Not Found", "code": 0})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---------------------------------------------------------------- assertions --
    def calls_to(self, suffix: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.calls if call[1].endswith(suffix)]


def _json(status_code: int, payload: Any, headers: dict[str, str] | None = None):
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def rate_limited(retry_after: float = 0.01, *, is_global: bool = False) -> httpx.Response:
    """A Discord 429 response."""

    return _json(
        429,
        {"message": "You are being rate limited.", "retry_after": retry_after, "global": is_global},
        headers={"X-RateLimit-Global": "true" if is_global else "false"},
    )


def server_error() -> httpx.Response:
    return _json(500, {"message": "Internal Server Error", "code": 0})


def _paginate(messages: list[dict[str, Any]], query: dict[str, Any]) -> list[dict[str, Any]]:
    """Mimic Discord's newest-first history pagination with a ``before`` cursor."""

    ordered = sorted(messages, key=lambda item: int(item["id"]), reverse=True)
    before = query.get("before")
    after = query.get("after")
    if before is not None:
        ordered = [item for item in ordered if int(item["id"]) < int(before)]
    if after is not None:
        ordered = [item for item in ordered if int(item["id"]) > int(after)]
    limit = int(query.get("limit", 50))
    return ordered[:limit]
