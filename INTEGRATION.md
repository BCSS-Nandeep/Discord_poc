# Integration Guide

For engineers integrating the Discord Collection Service into another application.

This service is a self-contained HTTP API. You do **not** need Discord credentials,
Discord libraries, or any knowledge of the Discord API in your application — you call
this service, it handles Discord.

- **Base URL** — wherever it is deployed, e.g. `http://discord-service:8100`
- **Auth** — `X-API-Key` header
- **Content type** — `application/json`
- **Interactive docs** — `GET /docs` (Swagger), `GET /openapi.json` (machine-readable)

---

## 1. Quick start

```bash
curl -H "X-API-Key: $KEY" http://localhost:8100/health
```

```json
{ "status": "ok", "service": "discord-service", "version": "1.0.0",
  "database": "connected", "discord": "connected", "auth": "enabled",
  "timestamp": "2026-09-06T07:30:00Z" }
```

Generate an OpenAPI client if you prefer:

```bash
curl http://localhost:8100/openapi.json -o discord-service.json
# openapi-generator generate -i discord-service.json -g <your-language>
```

---

## 2. Authentication

Set `API_KEYS` on the service to a comma-separated list — issue one key per consuming
application so they can be revoked independently:

```bash
API_KEYS=dsk_teamA_xxxxxxxx,dsk_teamB_yyyyyyyy
```

Send it on every `/discord/*` request:

```
X-API-Key: dsk_teamA_xxxxxxxx
```

Generate a key:

```bash
python -c "from app.core.security import generate_api_key; print(generate_api_key())"
```

| Endpoint | Key required |
|---|---|
| `GET /health` | No — for load balancers and uptime probes |
| `GET /ui`, `/docs`, `/openapi.json` | No |
| **everything under `/discord/*`** | **Yes** |

> If `API_KEYS` is empty the API is **open**. That is the local-development default; the
> service logs a warning at startup and `/health` reports `"auth": "disabled"`.
> Never deploy that way.

---

## 3. Error handling

Every error returns the same shape. Handle `code`, not the human message.

```json
{ "code": "ACCESS_NOT_GRANTED",
  "message": "The bot cannot collect from this channel yet",
  "details": { "channel_id": "223...", "reason": "BOT_CANNOT_VIEW_CHANNEL" } }
```

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| 401 | `UNAUTHORIZED` | Missing/invalid `X-API-Key` | Fix the key. Do not retry. |
| 404 | `NOT_FOUND` | Unknown local resource | Do not retry. |
| 404 | `DISCORD_NOT_FOUND` | Channel/guild gone or invisible | Do not retry. |
| 409 | `ACCESS_NOT_GRANTED` | Bot cannot read that channel yet | Create an access request; poll. |
| 409 | `INVALID_STATE_TRANSITION` | Illegal state change | Fix the call. |
| 422 | `VALIDATION_ERROR` | Bad input (usually a name where an id is required) | Fix the input. |
| 429 | `DISCORD_RATE_LIMITED` | Discord rate limit | Retry after `details.retry_after` seconds. |
| 502 | `DISCORD_UNAUTHORIZED` | Bot token rejected by Discord | Operator must fix. Do not retry. |
| 502 | `DISCORD_SERVER_ERROR` | Discord is unwell | Retry with backoff. |
| 503 | `DISCORD_NOT_CONFIGURED` | No bot token set | Operator must fix. |
| 504 | `DISCORD_TRANSPORT_ERROR` | Network/timeout to Discord | Retry with backoff. |

**Retry only** 429, 502 (`DISCORD_SERVER_ERROR`), 504 and 5xx. The service already
retries Discord internally with bounded backoff, so keep your own retries modest.

---

## 4. IDs

Every `channel_id` / `guild_id` is a **Discord snowflake** — a numeric string, e.g.
`223456789012345678`. Names are rejected with `422 VALIDATION_ERROR`.

To go from a name to an id:

```bash
GET /discord/guilds/search?query=threat
GET /discord/channels/search?query=incident&guild_id=123456789012345678
```

Treat ids as **strings**, never as integers — they exceed 2^53 and will silently
corrupt in JavaScript and some JSON parsers.

---

## 5. Pagination

List endpoints take `limit` (1–500, default 50) and `offset`, and return:

```json
{ "items": [ ... ],
  "pagination": { "total": 1280, "limit": 50, "offset": 0, "returned": 50 } }
```

Page until `offset + returned >= total`.

---

## 6. The private-channel workflow

The one concept worth understanding before integrating.

**Discord has no API for a bot to request access to a private channel.** There is no
endpoint to submit a request and no Discord-side approval. An "access request" here is
an **internal record** in this service's database meaning *"we want this channel;
re-check every 12 hours until a human Discord admin grants the bot permission."*

`ACCEPTED` means **our own re-check found the bot can genuinely read the channel** — not
that Discord approved anything. Do not present it to users as a Discord approval.

```
POST /discord/channels/{id}/access-request
        │
   ┌────┴─────┐
   │          │
accessible   not accessible
   │          │
   ▼          ▼
already    status PENDING, next_check_at = now + 12h
accessible        │
   │       a Discord admin grants the bot permission (outside this service)
   │              │
   │       12-hour worker (or POST .../recheck) re-evaluates
   │              │
   └──────────► ACCEPTED → history collected → monitor started
```

**Integration pattern:** create the request, then poll
`GET /discord/access-requests/{id}` (or the notifications feed) until `status` leaves
`PENDING`. Do not poll faster than a few minutes — the authoritative re-check is every
12 hours, and `POST .../recheck` exists for on-demand checks.

Terminal statuses: `ACCEPTED`, `DENIED`, `EXPIRED`, `CANCELLED`. `ERROR` and `PENDING`
are non-terminal.

---

## 7. Typical flows

### Collect from a channel

```bash
# 1. Can we read it?
curl -H "X-API-Key: $KEY" $BASE/discord/channels/$CH
#    -> access.collection_allowed: true | false

# 2a. If true, collect.
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"limit": 1000, "keywords": {"keywords": ["ransomware"]}}' \
  $BASE/discord/channels/$CH/scrape

# 2b. If false, open an access request and poll.
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"collect_history_on_grant": true, "monitor_on_grant": true}' \
  $BASE/discord/channels/$CH/access-request
```

### Keep a channel current

```json
POST /discord/channels/{id}/scrape
{ "incremental": true }
```

Resumes from the newest message already stored. Safe to run on a schedule — duplicates
are rejected by a unique index on `(guild_id, channel_id, message_id)`.

### Live monitoring

```json
POST /discord/channels/{id}/monitor/start
{ "keywords": {"keywords": ["ransomware"]}, "collect_history": true }
```

If the channel is private the monitor is created as `WAITING_FOR_ACCESS` and an access
request is opened automatically; it becomes `RUNNING` once access is granted.

### Search what was collected

```json
POST /discord/search
{ "keywords": ["ransomware", "credential"],
  "channel_id": "223456789012345678",
  "match_mode": "substring",
  "since": "2026-01-01T00:00:00Z",
  "limit": 100 }
```

Reads local storage only — never calls Discord, so it is fast and not rate limited.
`author_id` filters to one account's posts.

---

## 8. Message schema

```json
{
  "platform": "discord",
  "guild_id": "123456789012345678",
  "channel_id": "223456789012345678",
  "message_id": "333456789012345678",
  "author_id": "444456789012345678",
  "author_name": "analyst",
  "author_is_bot": false,
  "content": "New ransomware sample observed.",
  "timestamp": "2026-09-05T11:59:00Z",
  "edited_at": null,
  "message_url": "https://discord.com/channels/123.../223.../333...",
  "reply_to_message_id": null,
  "has_attachments": false,
  "attachments": [],
  "embeds": [],
  "matched_keywords": ["ransomware"],
  "source": "rest",
  "collected_at": "2026-09-05T12:00:00Z"
}
```

`source` is `rest` (historical) or `gateway` (live). All timestamps are ISO-8601 UTC.
`(guild_id, channel_id, message_id)` is unique, so it is a safe idempotency key.

---

## 9. Consuming events

`GET /discord/notifications` is an append-only feed of state transitions:
`ACCESS_REQUEST_CREATED`, `ACCESS_PENDING`, `ACCESS_GRANTED`, `ACCESS_DENIED`,
`ACCESS_CHECK_FAILED`, `ACCESS_REVOKED`, `MONITOR_STARTED`, `MONITOR_STOPPED`,
`MESSAGE_MATCHED`, `SYSTEM_ERROR`.

Poll with `since` (ISO-8601) and keep the newest `created_at` you have seen:

```bash
GET /discord/notifications?since=2026-09-06T07:00:00Z&limit=100
```

There is no push/webhook delivery. If you need one, say so — it is a contained addition.

---

## 10. Client examples

### Python

```python
import httpx

class DiscordCollectionClient:
    """Minimal client. Ids are strings; never coerce them to int."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def _request(self, method: str, path: str, **kwargs):
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            body = response.json() if response.content else {}
            raise DiscordServiceError(
                body.get("code", "UNKNOWN"),
                body.get("message", response.text),
                body.get("details"),
                response.status_code,
            )
        return response.json()

    def health(self):
        return self._request("GET", "/health")

    def guilds(self, refresh: bool = False):
        return self._request("GET", "/discord/guilds", params={"refresh": refresh})["items"]

    def channel_access(self, channel_id: str):
        return self._request("GET", f"/discord/channels/{channel_id}")["access"]

    def request_access(self, channel_id: str, *, monitor: bool = False, keywords=None):
        return self._request(
            "POST", f"/discord/channels/{channel_id}/access-request",
            json={
                "collect_history_on_grant": True,
                "monitor_on_grant": monitor,
                "keywords": {"keywords": keywords} if keywords else None,
            },
        )

    def scrape(self, channel_id: str, *, limit: int = 1000, incremental: bool = False):
        return self._request(
            "POST", f"/discord/channels/{channel_id}/scrape",
            json={"limit": limit, "incremental": incremental},
        )

    def search(self, keywords: list[str], **filters):
        return self._request(
            "POST", "/discord/search", json={"keywords": keywords, **filters}
        )


class DiscordServiceError(RuntimeError):
    RETRYABLE = {"DISCORD_RATE_LIMITED", "DISCORD_SERVER_ERROR", "DISCORD_TRANSPORT_ERROR"}

    def __init__(self, code, message, details, status):
        super().__init__(f"{code}: {message}")
        self.code, self.details, self.status = code, details or {}, status

    @property
    def retryable(self) -> bool:
        return self.code in self.RETRYABLE


# usage
client = DiscordCollectionClient("http://localhost:8100", api_key="dsk_...")
access = client.channel_access("223456789012345678")
if access["collection_allowed"]:
    print(client.scrape("223456789012345678", incremental=True))
else:
    print(client.request_access("223456789012345678", monitor=True)["message"])
```

### TypeScript

```ts
export class DiscordCollectionClient {
  constructor(private baseUrl: string, private apiKey: string) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(this.baseUrl.replace(/\/$/, "") + path, {
      method,
      headers: {
        "X-API-Key": this.apiKey,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw Object.assign(new Error(data?.message ?? `HTTP ${response.status}`), {
        code: data?.code, details: data?.details, status: response.status,
      });
    }
    return data as T;
  }

  health() { return this.request("GET", "/health"); }
  guilds() { return this.request("GET", "/discord/guilds"); }
  channelAccess(id: string) { return this.request("GET", `/discord/channels/${id}`); }
  scrape(id: string, limit = 1000) {
    return this.request("POST", `/discord/channels/${id}/scrape`, { limit });
  }
  search(keywords: string[], filters: Record<string, unknown> = {}) {
    return this.request("POST", "/discord/search", { keywords, ...filters });
  }
}
```

> Ids exceed `Number.MAX_SAFE_INTEGER`. Keep them as strings throughout — never
> `parseInt` a snowflake.

---

## 11. Deployment notes

| Concern | Guidance |
|---|---|
| Auth | Set `API_KEYS`. One key per consumer. |
| Transport | Terminate TLS at a reverse proxy — the service speaks plain HTTP. |
| CORS | `CORS_ORIGINS` (comma-separated) only if a browser calls it directly. |
| Storage | SQLite file at `DATABASE_URL`. Back it up; it holds message content. |
| Scaling | Single writer. Run **one** instance; `--workers > 1` is guarded by a lock so only one process runs the Gateway and the reconciler. |
| Health | `GET /health` — no key needed. `status` is `ok` \| `degraded` \| `error`. |
| Logs | `LOG_JSON=true` for structured logs. Secrets are scrubbed from every record. |
| Timeouts | A large `scrape` can run for minutes. Use a client timeout of 60s+ or a smaller `limit`. |

### What the operator must do in Discord

Nothing in this API can substitute for these:

1. Create the bot and enable the **Message Content** intent.
2. Invite the bot with `scope=bot` and **View Channel** + **Read Message History**
   (`permissions=66560`).
3. For each private channel, grant the bot access **manually in Discord**.

---

## 12. Limits worth knowing before you design around them

- **No global user search.** Discord provides no user-directory API to bots. You can
  look up what a known `author_id` posted, not find an account by username.
- **No server discovery.** The bot only sees servers it has been invited to.
- **Private channels need a human.** No API call can grant the bot access.
- **Message Content is privileged.** Without that intent, `content` is empty.
- **History is capped at 100/request** by Discord; the service paginates for you.
- **Notifications are pull-based.** No webhooks today.
