# Discord Collection Service

A standalone FastAPI service that collects Discord guild, channel and message data
through the **official Discord Bot API** — REST for historical resources, Gateway for
live events — and persists everything to SQLite.

It is a self-contained module: no Telegram code, models, credentials or dependencies
are involved anywhere.

---

## Table of contents

1. [Purpose](#1-purpose)
2. [Architecture](#2-architecture)
3. [Discord Developer Portal setup](#3-discord-developer-portal-setup)
4. [Creating the bot](#4-creating-the-bot)
5. [Required Gateway intents](#5-required-gateway-intents)
6. [Required permissions](#6-required-permissions)
7. [Environment configuration](#7-environment-configuration)
8. [Running locally](#8-running-locally)
9. [Adding the bot to a server](#9-adding-the-bot-to-a-server)
10. [How private-channel access works](#10-how-private-channel-access-works)
11. [Our access request is *not* a Discord approval](#11-our-access-request-is-not-a-discord-approval)
12. [REST API examples](#12-rest-api-examples)
13. [Monitoring behaviour](#13-monitoring-behaviour)
14. [SQLite schema overview](#14-sqlite-schema-overview)
15. [Troubleshooting](#15-troubleshooting)
16. [Security notes](#16-security-notes)

---

## 1. Purpose

Collect and search Discord message data for channels a bot is permitted to read:

- Discover the guilds and channels the bot can see.
- Evaluate, per channel, whether the bot can actually **view** it and **read its
  message history**, and report precisely *why* when it cannot.
- Track an **application-level access request** for private channels, re-checked every
  12 hours until a Discord server administrator grants the bot permission.
- Collect historical messages over the REST API with pagination and rate-limit handling.
- Monitor channels live over the Gateway.
- Filter by keyword at collection time and search what has been stored.
- Record every access-status transition as an application notification.

Everything uses officially supported bot mechanisms. There are no self-bots, no user
tokens, and nothing that attempts to bypass Discord permissions.

---

## 2. Architecture

Strict layering; each layer only talks to the one below it.

```
HTTP  ─►  app/api/routes_*.py        validation and orchestration only
          app/api/deps.py            request-scoped session + service graph
            │
            ▼
          app/discord/*_service.py   business logic
            │
            ▼
          app/database/repositories/ persistence only (the sole SQLAlchemy users)
            │
            ▼
          SQLite

          app/discord/rest_client.py the ONLY component that calls Discord over HTTP
          app/discord/gateway.py     the ONLY component holding a Gateway connection
          app/workers/               the 12-hour reconciliation worker
```

### Responsibilities

| Component | Responsibility |
|---|---|
| `DiscordRestClient` | Every Discord HTTP call. Auth, pinned API version, rate limits, retries, error translation. |
| `GatewayService` | Gateway connection lifecycle and real-time events. |
| `PermissionService` | The single place effective channel access is decided. |
| `GuildService` | Guild discovery and persistence. |
| `ChannelService` | Channel discovery, metadata, stored access state. |
| `MessageService` | Historical collection, normalization, persistence. |
| `SearchService` | Keyword search over stored messages (never calls Discord). |
| `MonitorService` | Monitor state machine. |
| `AccessRequestService` | Access-request records and their validated transitions. |
| `AccessWorkflowService` | Coordinates evaluation → request → acceptance → collection → monitoring. |
| `NotificationService` | Application-level status events. |
| `DiscordClientManager` | Composition root; builds a service graph per unit of work. |
| Repositories | SQLite persistence only. No business rules. |

`AccessWorkflowService` exists so the services never need to import each other. It is
the one place that spans several domains, which keeps the dependency graph acyclic.

### Project tree

```
discord_service/
├── app/
│   ├── main.py                     app factory, lifespan, error handlers
│   ├── api/
│   │   ├── deps.py                 DI: settings, database, service graph, pagination
│   │   ├── routes_discord.py       all /discord/* endpoints
│   │   ├── routes_auth.py          Discord OAuth2 login
│   │   ├── routes_health.py        GET /health
│   │   └── routes_ui.py            serves all three browser UIs
│   ├── core/
│   │   ├── config.py               pydantic-settings configuration
│   │   ├── security.py             X-API-Key / session authentication
│   │   ├── sessions.py             signed session cookies (stdlib hmac)
│   │   ├── enums.py                status enums + validated state machines
│   │   ├── exceptions.py           typed exception hierarchy
│   │   ├── logging.py              structured logging + secret redaction
│   │   └── runlock.py              single-instance lock for background workers
│   ├── discord/
│   │   ├── client.py               composition root, Gateway callbacks
│   │   ├── rest_client.py          Discord REST API client
│   │   ├── gateway.py              Discord Gateway listener
│   │   ├── guild_service.py
│   │   ├── channel_service.py
│   │   ├── message_service.py
│   │   ├── permission_service.py
│   │   ├── access_request_service.py
│   │   ├── monitor_service.py
│   │   ├── notification_service.py
│   │   ├── search_service.py
│   │   ├── oauth_service.py        Discord login flow
│   │   ├── keywords.py             keyword config + matcher
│   │   └── normalize.py            Discord payload → internal schema
│   ├── database/
│   │   ├── database.py             async engine, sessions, SQLite pragmas
│   │   ├── models.py               ORM models
│   │   └── repositories/
│   │       ├── base.py
│   │       ├── guild_repository.py
│   │       ├── channel_repository.py
│   │       ├── access_request_repository.py
│   │       ├── message_repository.py
│   │       ├── monitor_repository.py
│   │       └── notification_repository.py
│   ├── schemas/                    Pydantic request/response models
│   │   ├── common.py  guild.py  channel.py  message.py
│   │   ├── access_request.py  monitor.py  notification.py  health.py
│   ├── ui/
│   │   ├── index.html              the guided console served at /ui
│   │   ├── explorer.html           the API explorer served at /explorer
│   │   └── console.html            the wire-log console served at /console
│   └── workers/
│       └── access_reconciler.py    the 12-hour reconciliation worker
├── tests/                          365 tests, no network access required
├── INTEGRATION.md                  guide for consuming applications
├── .env.example
├── pyproject.toml
├── requirements.txt
├── run.py
└── README.md
```

---

## 3. Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications>.
2. **New Application** → give it a name → **Create**.
3. On **General Information**, copy the **Application ID** →
   `DISCORD_APPLICATION_ID`.

---

## 4. Creating the bot

1. In your application, open the **Bot** tab.
2. **Reset Token** → copy it → `DISCORD_BOT_TOKEN`.
   The token is shown **once**. Treat it like a password.
3. Turn **Public Bot** off unless you intend anyone to be able to invite it.

---

## 5. Required Gateway intents

Under **Bot → Privileged Gateway Intents**:

| Intent | Required | Why |
|---|---|---|
| **Message Content** | **Yes** | Without it, message `content` arrives empty. |
| Server Members | Optional | Only needed to compute permission bits via `GET /guilds/{id}/members/{user_id}`. |
| Presence | No | Not used. |

Non-privileged intents the service requests automatically: `guilds` (channel and
permission events) and `guild_messages` (new messages).

Set the matching flags in your environment so the service knows what it has:

```
DISCORD_MESSAGE_CONTENT_INTENT=true
DISCORD_GUILD_MEMBERS_INTENT=false
```

If `DISCORD_MESSAGE_CONTENT_INTENT=true` but the intent is **not** enabled in the
portal, the Gateway refuses to connect. The service logs a clear error and keeps
serving HTTP rather than crashing.

---

## 6. Required permissions

Per channel the bot must have:

- **View Channel** (`VIEW_CHANNEL`, `1 << 10`)
- **Read Message History** (`READ_MESSAGE_HISTORY`, `1 << 16`)

Permission integer for an invite URL: `66560`.

The service needs no write permissions — it never posts, edits or deletes anything.

---

## 6a. API authentication

Consuming applications authenticate with a shared key in the `X-API-Key` header.

```bash
API_KEYS=dsk_teamA_xxxxxxxx,dsk_teamB_yyyyyyyy
```

Issue one key per consumer so they can be revoked independently. Generate one with:

```bash
python -c "from app.core.security import generate_api_key; print(generate_api_key())"
```

| Endpoint | Key required |
|---|---|
| `GET /health` | No — so load balancers can probe it |
| `GET /ui`, `/docs`, `/openapi.json` | No |
| everything under `/discord/*` | **Yes** |

If `API_KEYS` is empty the API is **open**. That is the local-development default; the
service logs a warning at startup and `/health` reports `"auth": "disabled"`. Do not
deploy that way.

### Discord login (optional)

People using the console can sign in with their Discord account instead of pasting an
API key. Both work at once: humans get a session cookie, server-to-server callers keep
using `X-API-Key`.

```bash
DISCORD_CLIENT_SECRET=          # Developer Portal -> OAuth2 -> Reset Secret
DISCORD_OAUTH_REDIRECT_URI=http://localhost:8100/auth/discord/callback
SESSION_SECRET=                 # python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Register the redirect URI under **OAuth2 -> General -> Redirects**; Discord requires a
byte-for-byte match.

| Endpoint | Purpose |
|---|---|
| `GET /auth/discord/login` | Redirect to Discord's consent screen |
| `GET /auth/discord/callback` | Verify `state`, exchange the code, start a session |
| `GET /auth/me` | Current session (always 200) |
| `POST /auth/logout` | Clear the session |

Scope is `identify` only. **No OAuth2 scope grants a third-party app access to message
history** — collection stays on the bot token, so login adds identity, not reach. The
access token is used once to read the profile, then revoked and discarded; only the
Discord user id and display name are stored, and the session cookie is signed but not
encrypted, so it never carries a token.

**Integrating from another application?** See **[INTEGRATION.md](INTEGRATION.md)** for
error codes, retry rules, the private-channel polling pattern, and ready-made Python and
TypeScript clients.

---

## 7. Environment configuration

Copy `.env.example` to `.env` and fill it in. **Never commit `.env`** (it is in
`.gitignore`).

### Required

| Variable | Description |
|---|---|
| `DISCORD_APPLICATION_ID` | Application ID from the Developer Portal. |
| `DISCORD_BOT_TOKEN` | Bot token. Never logged, never returned by the API. |
| `DISCORD_API_BASE_URL` | Pinned API base. Must start with `https://discord.com/api`. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./discord_service.db` | SQLite only; upgraded to `aiosqlite` automatically. |
| `ACCESS_RECHECK_HOURS` | `12` | Hours between automatic re-checks of pending requests. |
| `ACCESS_REQUEST_EXPIRY_DAYS` | `30` | A pending request older than this becomes `EXPIRED`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `LOG_JSON` | `false` | Newline-delimited JSON logs. |
| `DISCORD_MESSAGE_CONTENT_INTENT` | `true` | Must match the Developer Portal. |
| `DISCORD_GUILD_MEMBERS_INTENT` | `false` | Must match the Developer Portal. |
| `ENABLE_GATEWAY` | `true` | Master switch for the Gateway listener. |
| `ENABLE_BACKGROUND_WORKERS` | `true` | Master switch for the reconciliation worker. |
| `WORKER_TICK_SECONDS` | `300` | How often the worker looks for due requests. |
| `WORKER_BATCH_SIZE` | `25` | Requests processed per tick. |
| `WORKER_LOCK_FILE` | `.discord_service.worker.lock` | Single-instance lock file. |
| `HISTORY_PAGE_SIZE` | `100` | Discord's maximum per request. |
| `HISTORY_MAX_MESSAGES` | `1000` | Default scrape ceiling. |
| `DISCORD_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout. |
| `DISCORD_MAX_RETRIES` | `3` | Bounded retries for transient failures. |
| `DISCORD_MAX_RATE_LIMIT_WAIT_SECONDS` | `60` | Refuse to sleep longer than this on a 429. |
| `HOST` / `PORT` | `0.0.0.0` / `8100` | Bind address. |
| `CORS_ORIGINS` | *(empty)* | Comma-separated origins. Empty disables CORS. |
| `API_KEYS` | *(empty)* | Comma-separated keys for `X-API-Key`. Empty disables auth. |
| `DISCORD_CLIENT_SECRET` | *(empty)* | OAuth2 secret. Enables Discord login. |
| `DISCORD_OAUTH_REDIRECT_URI` | `http://localhost:8100/auth/discord/callback` | Must match the portal exactly. |
| `DISCORD_OAUTH_SCOPES` | `identify` | Space-separated OAuth2 scopes. |
| `SESSION_SECRET` | *(empty)* | Signs session cookies. Required for login. |
| `SESSION_TTL_HOURS` | `12` | Session lifetime. |
| `SESSION_COOKIE_SECURE` | `false` | Send cookies over HTTPS only. |

---

## 8. Running locally

```bash
cd discord_service
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env
```

**Start the service:**

```bash
python run.py
```

Equivalent explicit form:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8100
```

Development auto-reload:

```bash
python run.py --reload
```

Then open:

- **Console — <http://localhost:8100/ui>** (start here; `/` redirects to it)
- **API Explorer — <http://localhost:8100/explorer>** (test any endpoint directly)
- **Wire-log Console — <http://localhost:8100/console>** (denser, single-page, with a live request log)
- Swagger UI — <http://localhost:8100/docs>
- ReDoc — <http://localhost:8100/redoc>
- Health — <http://localhost:8100/health>

### The browser console

`/ui` is a single-page console for driving the API by hand, meant for understanding the
service rather than for production operation. Six tabs follow the natural workflow:

1. **Servers** — discover and search the guilds the bot was invited to.
2. **Channels & Access** — list channels with colour-coded access badges, and run a
   full access evaluation on one channel.
3. **Access Requests** — create requests for private channels, re-check immediately,
   cancel. Explains that `ACCEPTED` is *our* re-check succeeding, not Discord approving.
4. **Collect & Search** — scrape history and search what was stored.
5. **Monitors** — start/stop monitoring and watch `WAITING_FOR_ACCESS → RUNNING`.
6. **Notifications** — the application event feed.

A **Last API call** panel at the bottom shows the method, path, status and response body
for every action, so the console doubles as live documentation of the REST API.

It is a thin client: no state, no business rules, only fetch calls against the
documented endpoints. All CSS and JS are inline — no CDN, no build step, works offline.
Because it is served by the application itself it is same-origin, so it needs no CORS
configuration.

**Tests, lint and types:**

```bash
python -m pytest tests/          # 238 tests, no Discord account needed
python -m ruff check app tests run.py
python -m mypy app --ignore-missing-imports
```

---

## 9. Adding the bot to a server

1. Developer Portal → your application → **OAuth2 → URL Generator**.
2. Scopes: **`bot`**.
3. Bot permissions: **View Channel** and **Read Message History**.
4. Open the generated URL and pick a server. You need **Manage Server** on it.

Or build the URL directly:

```
https://discord.com/api/oauth2/authorize
  ?client_id=<DISCORD_APPLICATION_ID>
  &scope=bot
  &permissions=66560
```

Confirm it worked:

```bash
curl http://localhost:8100/discord/guilds?refresh=true
```

---

## 10. How private-channel access works

A **private channel** is one where the `@everyone` role is denied *View Channel*;
visibility is granted back to specific roles or members. A bot invited to the server
still cannot see such a channel until an administrator grants it access.

```
  POST /discord/channels/{id}/access-request
              │
              ▼
   Evaluate the bot's real access
              │
      ┌───────┴────────┐
      │                │
  accessible      not accessible
      │                │
      ▼                ▼
  return         create access request
  ACCEPTED       status = PENDING
  (no request    next_check_at = now + 12h
   created)      notify ACCESS_REQUEST_CREATED
                        + ACCESS_PENDING
                 report PRIVATE / ACCESS_PENDING
                        │
                        ▼
         ┌──────────────────────────────┐
         │  A Discord server admin      │
         │  grants the bot              │
         │  View Channel +              │
         │  Read Message History        │
         │  — outside this service      │
         └──────────────┬───────────────┘
                        │
      ┌─────────────────┴──────────────────┐
      │                                    │
 12-hour worker                   Gateway permission event
 (authoritative)                  (fast path, optional)
      │                                    │
      └─────────────────┬──────────────────┘
                        ▼
              Re-evaluate real access
                        │
         ┌──────────────┼────────────────┐
         │              │                │
    still no       granted          channel gone /
    access                          not a text channel
         │              │                │
         ▼              ▼                ▼
   stays PENDING   status=ACCEPTED    status=DENIED
   check_count++   accepted_at=now    rejection_reason
   next_check_at   notify ACCESS_GRANTED
   = now + 12h     collect history
                   start monitor (if requested)
```

### Access evaluation

`PermissionService` combines two signals:

1. **Discord's own enforcement (authoritative).** `GET /channels/{id}` answers 403 when
   the bot lacks *View Channel*; `GET /channels/{id}/messages` answers 403 when it
   lacks *Read Message History*. This needs no privileged intent and reflects exactly
   what a collection run would experience.
2. **Computed permission bits (explanatory).** Channel `permission_overwrites` are
   resolved with Discord's documented algorithm to explain *why* a channel is private.

It always returns a structured result:

```json
{
  "channel_id": "223456789012345678",
  "is_private": true,
  "bot_can_view": false,
  "bot_can_read_history": false,
  "access_status": "PRIVATE",
  "reason": "BOT_CANNOT_VIEW_CHANNEL",
  "collection_allowed": false
}
```

`reason` distinguishes: `FULLY_ACCESSIBLE`, `CHANNEL_NOT_FOUND`,
`BOT_CANNOT_VIEW_CHANNEL`, `MISSING_READ_MESSAGE_HISTORY`,
`MESSAGE_CONTENT_NOT_ENABLED`, `NOT_A_TEXT_CHANNEL`, `INVALID_BOT_TOKEN`,
`RATE_LIMITED`, `DISCORD_ERROR`.

A **rate limit or outage never changes stored state.** Those are transient; the request
stays `PENDING`, an `ACCESS_CHECK_FAILED` notification is recorded, and the worker tries
again. An `ACCEPTED` request is never downgraded because of a temporary failure.

---

## 11. Our access request is *not* a Discord approval

**Discord provides no API for a bot to request access to a private channel, and no
Discord-side approval to wait on.**

What this service calls an "access request" is an **internal record of intent** stored
in our own SQLite database. It means:

> *We want to collect from this channel. The bot cannot read it yet. Re-check every 12
> hours until a human administrator grants permission in Discord.*

Concretely:

- Nothing is sent to Discord when a request is created.
- Discord never "accepts" or "rejects" the request.
- `ACCEPTED` means **our own re-check found that the bot can now genuinely read the
  channel** — not that Discord approved anything.
- `DENIED` is set only for a definite, non-transient condition we observed (the channel
  no longer exists, or it cannot hold collectable messages).
- The service never sends Discord DMs and never invents a Discord-native approval
  notification.

The only way access is granted is a Discord server administrator giving the bot
*View Channel* and *Read Message History*, in Discord, by hand.

---

## 12. REST API examples

All examples assume `BASE=http://localhost:8100`.

### Health

```bash
curl $BASE/health
```

```json
{
  "status": "ok",
  "service": "discord-service",
  "version": "1.0.0",
  "database": "connected",
  "discord": "connected",
  "timestamp": "2026-09-05T12:00:00Z"
}
```

`GET /discord/health` adds bot identity, Gateway status, worker status and row counts.

### Discovery

Every endpoint that takes a `channel_id` or `guild_id` needs Discord's numeric
**snowflake**, not a name. To go from a name to an id, either turn on Developer Mode in
Discord (User Settings -> Advanced) and right-click -> **Copy Channel ID**, or use the
name-search endpoints below.

```bash
curl "$BASE/discord/bot"
curl "$BASE/discord/guilds?refresh=true"
curl "$BASE/discord/guilds/123456789012345678"
curl "$BASE/discord/guilds/123456789012345678/channels"
curl "$BASE/discord/guilds/123456789012345678/channels?only_private=true"
curl "$BASE/discord/channels/223456789012345678"
```

### Find things by name instead of id

```bash
# Which servers match "threat"?
curl "$BASE/discord/guilds/search?query=threat&refresh=true"

# Which channels are called something like "incident"?
curl "$BASE/discord/channels/search?query=incident"

# Discover a guild's channels and search them in one call:
curl "$BASE/discord/channels/search?query=general&guild_id=123456789012345678&refresh=true"
```

Both search stored rows, so a channel is only findable after its guild has been
discovered (`GET /discord/guilds/{id}/channels`, or `refresh=true` above). Matching is
case-insensitive substring.

Only servers the bot has been **invited to** are searchable. Discord provides no way
for a bot to discover or search servers it is not a member of, and no global user
search exists either — see [Troubleshooting](#15-troubleshooting).

### Request access to a private channel

```bash
curl -X POST "$BASE/discord/channels/223456789012345678/access-request" \
  -H 'Content-Type: application/json' \
  -d '{
        "collect_history_on_grant": true,
        "monitor_on_grant": true,
        "keywords": {"keywords": ["ransomware", "malware", "credential"]},
        "requested_by": "threat-intel-team"
      }'
```

```json
{
  "channel_id": "223456789012345678",
  "channel_name": "incident-response",
  "channel_status": "PRIVATE",
  "access_request_status": "PENDING",
  "requested_at": "2026-09-05T12:00:00Z",
  "next_check_at": "2026-09-06T00:00:00Z",
  "created": true,
  "message": "Private channel. Waiting for a Discord server administrator to grant the bot access (View Channel + Read Message History)."
}
```

If the bot can already read the channel, no request is created and
`already_accessible` is `true`.

### Track and re-check requests

```bash
curl "$BASE/discord/access-requests?status=PENDING"
curl "$BASE/discord/access-requests/12"

# Do not wait for the 12-hour cycle:
curl -X POST "$BASE/discord/access-requests/12/recheck"

# Stop re-checking:
curl -X POST "$BASE/discord/access-requests/12/cancel"
```

### Historical collection

```bash
curl -X POST "$BASE/discord/channels/223456789012345678/scrape" \
  -H 'Content-Type: application/json' \
  -d '{"limit": 500, "keywords": {"keywords": ["ransomware"]}}'
```

```json
{
  "channel_id": "223456789012345678",
  "fetched": 500, "stored": 500, "duplicates": 0,
  "matched": 12, "pages": 5, "completed": true
}
```

Collect only what is new since the last run:

```bash
curl -X POST "$BASE/discord/channels/223456789012345678/scrape" \
  -H 'Content-Type: application/json' -d '{"incremental": true}'
```

Without access this returns **409**:

```json
{
  "code": "ACCESS_NOT_GRANTED",
  "message": "The bot cannot collect from this channel yet",
  "details": {
    "reason": "BOT_CANNOT_VIEW_CHANNEL",
    "hint": "Create an access request and ask a Discord server administrator to grant the bot access."
  }
}
```

### Read and search stored messages

```bash
curl "$BASE/discord/channels/223456789012345678/messages?limit=50"

curl -X POST "$BASE/discord/search" \
  -H 'Content-Type: application/json' \
  -d '{
        "channel_id": "223456789012345678",
        "keywords": ["ransomware", "malware", "credential"],
        "limit": 100
      }'
```

### Monitoring

```bash
curl -X POST "$BASE/discord/channels/223456789012345678/monitor/start" \
  -H 'Content-Type: application/json' \
  -d '{"keywords": {"keywords": ["ransomware"]}, "collect_history": true}'

curl "$BASE/discord/channels/223456789012345678/monitor/status"
curl -X POST "$BASE/discord/channels/223456789012345678/monitor/stop" \
  -H 'Content-Type: application/json' -d '{"reason": "done"}'
```

### Notifications

```bash
curl "$BASE/discord/notifications?unread=true"
curl "$BASE/discord/notifications?event_type=ACCESS_GRANTED"
curl "$BASE/discord/notifications?channel_id=223456789012345678"
curl -X POST "$BASE/discord/notifications/42/read"
```

### Endpoint summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/ui` | Browser console (not in the OpenAPI schema). |
| GET | `/explorer` | API explorer -- test any endpoint (not in the OpenAPI schema). |
| GET | `/console` | Wire-log console -- tables, detail panel, live request log (not in the OpenAPI schema). |
| GET | `/health` | Service health. |
| GET | `/discord/health` | Discord subsystem health. |
| GET | `/discord/bot` | Authenticated bot identity. |
| GET | `/discord/guilds` | List guilds. |
| GET | `/discord/guilds/search` | **Find guilds by name.** |
| GET | `/discord/guilds/{guild_id}` | Get a guild. |
| GET | `/discord/guilds/{guild_id}/channels` | List channels. |
| GET | `/discord/channels/search` | **Find channels by name.** |
| GET | `/discord/channels/{channel_id}` | Channel + access evaluation. |
| POST | `/discord/channels/{channel_id}/access-request` | Create an access request. |
| GET | `/discord/access-requests` | List access requests. |
| GET | `/discord/access-requests/{request_id}` | Get an access request. |
| POST | `/discord/access-requests/{request_id}/recheck` | Immediate re-check. |
| POST | `/discord/access-requests/{request_id}/cancel` | Cancel a request. |
| GET | `/discord/channels/{channel_id}/messages` | List stored messages. |
| POST | `/discord/channels/{channel_id}/scrape` | Collect history. |
| POST | `/discord/search` | Search stored messages. |
| POST | `/discord/channels/{channel_id}/monitor/start` | Start monitoring. |
| POST | `/discord/channels/{channel_id}/monitor/stop` | Stop monitoring. |
| POST | `/discord/guilds/{guild_id}/monitoring` | Guild-level monitoring master switch. |
| GET | `/discord/channels/{channel_id}/monitor/status` | Monitor status. |
| GET | `/discord/monitors` | List monitors. |
| GET | `/discord/notifications` | List notifications. |
| POST | `/discord/notifications/{id}/read` | Mark notification read. |

---

## 13. Monitoring behaviour

A monitor only reaches `RUNNING` once access is confirmed.

```
private channel
      │  monitor requested
      ▼
WAITING_FOR_ACCESS  ── access request opened automatically
      │  administrator grants permission
      ▼
historical collection
      │
      ▼
  STARTING ──► RUNNING ──► live messages via the Gateway
```

Valid transitions are enforced; `STOPPED → RUNNING` and
`WAITING_FOR_ACCESS → RUNNING` are rejected, so a monitor cannot skip the access check.
If access is later revoked, the monitor moves explicitly back to `WAITING_FOR_ACCESS`
with an `ACCESS_REVOKED` notification rather than failing silently.

### Live message handling

For each Gateway message on a monitored channel the service:

1. Confirms the channel has a `RUNNING` monitor.
2. Normalizes the payload into the same internal schema the REST scraper produces.
3. Applies the monitor's keyword configuration.
4. Stores the message, skipping duplicates via the unique index.
5. Records a `MESSAGE_MATCHED` notification when a keyword matched.
6. Updates `last_event_at`, `last_message_id` and the seen/matched counters.

The bot's own messages are always ignored.

### Gateway as a fast path for permission changes

`on_guild_channel_update`, `on_guild_role_update` and `on_member_update` (for the bot
itself) signal that permissions may have changed, so pending access requests are
re-checked immediately instead of waiting up to 12 hours. **The 12-hour reconciler
remains authoritative** — the Gateway only shortens the wait, and the service is fully
correct with `ENABLE_GATEWAY=false`.

### The 12-hour reconciliation worker

Every `WORKER_TICK_SECONDS` (default 300s) the worker:

1. Expires pending requests older than `ACCESS_REQUEST_EXPIRY_DAYS`.
2. Selects `PENDING` requests where `next_check_at <= now`, up to `WORKER_BATCH_SIZE`.
3. **Atomically claims** each one with a conditional `UPDATE`, which also pushes
   `next_check_at` forward and increments `check_count`.
4. Re-evaluates real access:
   - **still inaccessible** → stays `PENDING`, `next_check_at = now + 12h`;
   - **access granted** → `ACCEPTED`, channel access fields updated,
     `ACCESS_GRANTED` notification, history collected, waiting monitor promoted;
   - **channel gone / not a text channel** → `DENIED` with a reason;
   - **transient failure** → stays `PENDING`, `ACCESS_CHECK_FAILED` notification.
5. Refreshes the Gateway's monitored-channel set if anything was granted.

The claim is the concurrency guard: two workers, or a worker racing a manual re-check,
cannot process the same request — the second `UPDATE` matches zero rows and is skipped.
This also makes a crash mid-check safe, since `next_check_at` has already moved forward.

**Duplicate workers.** `uvicorn --reload` runs a child process and respawns it on every
edit; `--workers N` forks N processes. An advisory OS lock file
(`WORKER_LOCK_FILE`) ensures only one process runs the Gateway and the reconciler —
the others serve HTTP only and log *"serving HTTP only"*. The lock is released
automatically when the process exits, even if it is killed.

### Historical scraping

`GET /channels/{id}/messages` returns at most **100** messages per call, so the service
pages backwards using the `before` cursor:

- `limit` bounds the **total** fetched, not the page size.
- `after` (or `incremental: true`) resumes from the newest message already stored.
- Duplicates are skipped by the database's unique index plus
  `INSERT ... ON CONFLICT DO NOTHING`, so re-running a scrape is always safe.
- 403/404/429 stop the run cleanly and report `stopped_reason`; pages already stored
  are kept.
- Collection only ever runs when access has been confirmed.

### Keyword filtering

```json
{"keywords": ["ransomware", "malware"], "match_mode": "substring",
 "case_sensitive": false, "store_non_matching": true}
```

- `substring` (default) — case-insensitive containment.
- `word` — whole words only (`cred` will not match `credentials`).
- `exact` — the whole message must equal the keyword.
- `store_non_matching: false` — store only messages that matched.

Matches are written to `matched_keywords_json` at collection time. The same matcher is
used by the REST scraper, the Gateway and `POST /discord/search`, so behaviour is
identical everywhere. Search uses parameterized SQL `LIKE` with escaped wildcards, then
re-applies the matcher per row.

---

## 14. SQLite schema overview

Six tables, created automatically at startup. WAL journaling and a busy timeout are
enabled so the worker can write while HTTP requests read.

**`discord_guilds`** — `id`, `guild_id` (unique), `name`, `icon_url`, `owner_id`,
`is_available`, `created_at`, `updated_at`.

**`discord_channels`** — `id`, `guild_id`, `channel_id` (unique), `parent_id`, `name`,
`channel_type`, `position`, `topic`, `is_private`, `bot_can_view`,
`bot_can_read_history`, `bot_can_read_message_content`, `access_status`,
`access_reason`, `last_permission_check_at`, `created_at`, `updated_at`.

**`discord_access_requests`** — `id`, `guild_id`, `channel_id`, `channel_name`,
`requested_at`, `status`, `last_checked_at`, `next_check_at`, `accepted_at`,
`denied_at`, `expires_at`, `rejection_reason`, `last_error`, `check_count`,
`requested_by`, `note`, `collect_history_on_grant`, `monitor_on_grant`,
`keyword_config_json`, `created_at`, `updated_at`.
A **partial unique index** on `channel_id WHERE status = 'PENDING'` prevents duplicate
open requests at the storage layer.

**`discord_messages`** — `id`, `guild_id`, `channel_id`, `message_id`, `author_id`,
`author_name`, `author_is_bot`, `content`, `timestamp`, `edited_at`, `message_url`,
`reply_to_message_id`, `has_attachments`, `attachments_json`, `embeds_json`,
`raw_json`, `matched_keywords_json`, `source`, `collected_at`, `created_at`,
`updated_at`.
Unique index `ux_discord_messages_identity` on **`(guild_id, channel_id, message_id)`**.
`guild_id` is `NOT NULL` (empty string fallback) because SQLite treats `NULL`s inside a
`UNIQUE` tuple as distinct, which would silently defeat duplicate protection.

**`discord_monitors`** — `id`, `guild_id`, `channel_id` (unique), `status`,
`keyword_config_json`, `store_all_messages`, `started_at`, `stopped_at`,
`last_event_at`, `last_message_id`, `last_error`, `messages_seen`, `messages_matched`,
`created_at`, `updated_at`.

**`discord_notifications`** — `id`, `access_request_id`, `guild_id`, `channel_id`,
`event_type`, `message`, `payload_json`, `created_at`, `delivered_at`, `read_at`.

Indexes cover `guild_id`, `channel_id`, `message_id`, `timestamp`, access-request
`status` and `next_check_at`, monitor `status`, and notification `created_at` /
`delivered_at` / `read_at`.

All timestamps are stored as UTC and always come back timezone-aware.

### Status values

| Domain | Values |
|---|---|
| Channel access | `UNKNOWN`, `PUBLIC_ACCESSIBLE`, `PRIVATE`, `ACCESSIBLE`, `ERROR` |
| Access request | `PENDING`, `ACCEPTED`, `DENIED`, `ERROR`, `EXPIRED`, `CANCELLED` |
| Monitor | `WAITING_FOR_ACCESS`, `STARTING`, `RUNNING`, `STOPPED`, `PAUSED`, `ERROR` |
| Notification | `ACCESS_REQUEST_CREATED`, `ACCESS_PENDING`, `ACCESS_GRANTED`, `ACCESS_DENIED`, `ACCESS_CHECK_FAILED`, `ACCESS_REVOKED`, `MONITOR_STARTED`, `MONITOR_STOPPED`, `MESSAGE_MATCHED`, `SYSTEM_ERROR` |

`PUBLIC_ACCESSIBLE` means visible to `@everyone`; `ACCESSIBLE` means the channel is
private but the bot has been granted access. Both allow collection.

### Message normalization

Both collection paths produce the same shape:

```json
{
  "platform": "discord",
  "guild_id": "...", "channel_id": "...", "message_id": "...",
  "author_id": "...", "author_name": "...",
  "content": "...", "timestamp": "...",
  "message_url": "https://discord.com/channels/<guild>/<channel>/<message>",
  "attachments": [], "embeds": [], "matched_keywords": []
}
```

`raw_json` keeps a trimmed subset of the original payload (ids, flags, mentions,
references) for auditing, without duplicating the normalized columns.

---

## 15. Troubleshooting

**`/health` says `"discord": "error"`**
The token was rejected. Check `DISCORD_BOT_TOKEN` and reset it in the Developer Portal
if needed. Look for `Discord rejected the bot token (401)` in the logs.

**`/health` says `"discord": "unconfigured"`**
`DISCORD_BOT_TOKEN` is empty. The service still starts so `/health` is reachable, but
every Discord endpoint returns **503**.

**Gateway status is `error` with a privileged-intent message**
Enable **Message Content** under Bot → Privileged Gateway Intents, or set
`DISCORD_MESSAGE_CONTENT_INTENT=false`. The service keeps serving HTTP either way.

**Messages are stored but `content` is empty**
The Message Content intent is not actually enabled. Enable it in the portal and
re-scrape.

**`GET /discord/guilds` returns an empty list**
The bot has not been invited anywhere yet. See
[Adding the bot to a server](#9-adding-the-bot-to-a-server), then call
`?refresh=true`.

**Scrape returns 409 `ACCESS_NOT_GRANTED`**
Expected for a channel the bot cannot read. Check `details.reason`:
- `BOT_CANNOT_VIEW_CHANNEL` — needs *View Channel*.
- `MISSING_READ_MESSAGE_HISTORY` — visible, but needs *Read Message History*.
- `MESSAGE_CONTENT_NOT_ENABLED` — enable the intent.

Create an access request and ask an administrator to grant the permissions.

**An access request stays `PENDING` forever**
Nobody has granted the bot access yet. Check `last_error` and `check_count`, verify the
permissions in Discord (channel → Edit Channel → Permissions), then force a re-check
with `POST /discord/access-requests/{id}/recheck`.

**"Value must follow pattern `^\d{1,20}$`" in Swagger**
You typed a channel or server *name* into an id field. Those fields need Discord's
numeric snowflake (e.g. `223456789012345678`). Use
`GET /discord/channels/search?query=<name>` to find the id, or enable Developer Mode in
Discord and right-click the channel -> **Copy Channel ID**.

**Can I search for a Discord user by username?**
No. Discord provides no global user-search endpoint for bots, by design — there is no
user directory API. You can look up what a known account posted with
`POST /discord/search` and an `author_id`, since every stored message records
`author_id`, `author_name` and `author_is_bot`.

**`database is locked`**
Another process is writing. WAL and a 10s busy timeout are enabled by default; raise
`DATABASE_BUSY_TIMEOUT_MS` if you run several instances against one file.

**Background work does not start**
Check the logs for `Worker lock already held by another process`. That is the
single-instance guard doing its job. If a previous run was killed, the OS releases the
lock automatically; if you moved `WORKER_LOCK_FILE`, make every instance agree on it.

**Two Gateway connections after enabling `--reload`**
Should not happen — the lock prevents it. Confirm all processes share the same
`WORKER_LOCK_FILE` and that it is on a local filesystem (advisory locks are unreliable
on some network shares).

**429s during a large scrape**
Normal. The client honours `Retry-After` with bounded retries. Lower
`HISTORY_MAX_MESSAGES`, or raise `DISCORD_MAX_RATE_LIMIT_WAIT_SECONDS` to tolerate
longer waits.

---

## 16. Security notes

**Secrets**

- The bot token is read only from the environment; it is never hardcoded.
- It is held in a `SecretStr`, so it cannot leak through `repr()`, a traceback or a
  model dump.
- A logging filter on the root logger scrubs the token from **every** log record,
  including records produced by third-party libraries.
- The token is never returned by any endpoint, never appears in the OpenAPI document,
  and `Authorization` headers are never logged.
- `.env` is git-ignored; `.env.example` contains placeholders only.

**Input validation**

- Every Discord id is validated as a numeric snowflake (1–20 digits) before it can
  reach a URL, so path traversal and endpoint injection are impossible.
- `DISCORD_API_BASE_URL` is hard-restricted to `https://discord.com/api`; a client can
  never point the service at another host.
- Pagination parameters are bounded (`limit` 1–500, `offset` ≥ 0).
- Keyword search uses parameterized SQL with escaped `LIKE` wildcards.
- Redirects are disabled on the HTTP client.

**Error handling**

- API errors are sanitized: a stable `code`, a human message and safe details. Stack
  traces and internals are logged server-side only.
- Validation errors report the location and type but never echo the submitted value.

**Discord conduct**

- Only the official bot/application model — no self-bots, no user tokens.
- Permission-denied errors are never retried in a loop.
- Rate limits are respected proactively (bucket headers) and reactively (`Retry-After`).
- The service requests read-only permissions and never posts, edits or deletes.
- Nothing attempts to bypass Discord permissions; a private channel stays inaccessible
  until an administrator grants access.

**Deployment**

- Set `CORS_ORIGINS` explicitly; CORS is disabled when empty.
- Set `API_KEYS` before exposing the service anywhere. With it empty, every
  `/discord/*` endpoint is open to whatever can reach the port.
- Terminate TLS at a reverse proxy — the service speaks plain HTTP.
- Restrict filesystem permissions on the SQLite file: it holds collected message
  content.
