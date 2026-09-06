"""FastAPI application factory, lifespan and error handling.

Startup order
-------------
1. Load configuration and configure logging (with secret redaction).
2. Initialize SQLite and create the schema.
3. Validate the Discord configuration and initialize the REST client.
4. Take the single-instance worker lock.
5. Start the Gateway listener (lock holder only).
6. Start the 12-hour reconciliation worker (lock holder only).
7. Recover RUNNING / WAITING_FOR_ACCESS monitors from SQLite.

Shutdown reverses this: worker, Gateway, HTTP client, database, lock.

The worker lock is what keeps ``uvicorn --reload`` (and ``--workers N``) from starting
duplicate Gateways or reconcilers: every process serves HTTP, but only the lock holder
runs background work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_discord import router as discord_router
from app.api.routes_health import SERVICE_VERSION
from app.api.routes_health import router as health_router
from app.api.routes_ui import router as ui_router
from app.core.config import Settings, get_settings
from app.core.enums import MonitorStatus
from app.core.exceptions import DiscordServiceError
from app.core.logging import configure_logging, get_logger, register_secret
from app.core.runlock import ProcessLock
from app.core.security import register_api_key_secrets
from app.database.database import Database
from app.discord.client import DiscordClientManager
from app.workers.access_reconciler import AccessReconciliationWorker

logger = get_logger(__name__)

DESCRIPTION = """
Standalone Discord data-collection service.

Collects Discord guild, channel and message data through the **official Discord Bot
API** -- REST for historical resources, Gateway for live events. Persists everything to
SQLite.

### Private channels

Discord provides **no API for a bot to request access to a private channel** and no
Discord-side approval flow. This service implements an *application-level* access
request instead:

1. You request access to a channel the bot cannot read.
2. We store a `PENDING` request and report the channel as `PRIVATE`.
3. A Discord **server administrator** grants the bot *View Channel* and
   *Read Message History* in Discord itself.
4. We re-check every 12 hours (and immediately on a Gateway permission event).
5. Once the bot really can read the channel the request becomes `ACCEPTED`, history is
   collected and monitoring starts.

Nothing here bypasses Discord permissions, and only bot tokens are used -- never user
tokens or self-bots.
"""


def _startup_banner(settings: Settings) -> None:
    logger.info(
        "Starting Discord collection service",
        extra={"version": SERVICE_VERSION, **settings.public_summary()},
    )


async def _recover_monitors(clients: DiscordClientManager, database: Database) -> None:
    """Restore monitor state from SQLite after a restart.

    A monitor that was RUNNING is re-registered with the Gateway.  A monitor stuck in
    STARTING (the process died mid-start) is moved back to WAITING_FOR_ACCESS so the
    reconciler can drive it forward rather than leaving it wedged.
    """

    async with database.session() as session:
        services = clients.build_services(session)
        monitors = await services.monitors.list_active()
        running = 0
        waiting = 0
        for monitor in monitors:
            if monitor.status == MonitorStatus.RUNNING.value:
                running += 1
            elif monitor.status == MonitorStatus.STARTING.value:
                await services.monitors.mark_waiting_for_access(monitor)
                waiting += 1
            else:
                waiting += 1
    logger.info(
        "Monitor state recovered",
        extra={"running": running, "waiting_for_access": waiting},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown."""

    settings: Settings = app.state.settings
    database: Database = app.state.database
    clients: DiscordClientManager = app.state.clients
    lock: ProcessLock = app.state.worker_lock

    _startup_banner(settings)

    if not settings.auth_enabled:
        logger.warning(
            "API authentication is DISABLED (API_KEYS is empty). Every /discord/* "
            "endpoint is open to anything that can reach this port. Set API_KEYS "
            "before exposing this service beyond localhost."
        )
    else:
        logger.info(
            "API authentication enabled",
            extra={"configured_keys": len(settings.api_key_list)},
        )

    # 1-2. Database.
    await database.create_all()

    # 3. Discord configuration and REST client.
    if not settings.discord_configured:
        logger.error(
            "DISCORD_BOT_TOKEN is not set. The service will start so /health is "
            "reachable, but every Discord endpoint will return 503."
        )
    await clients.start_rest()
    if settings.discord_configured:
        try:
            await clients.verify_bot()
        except Exception as exc:  # noqa: BLE001 - never block startup on Discord
            logger.error(
                "Could not authenticate with Discord at startup",
                extra={"error_type": type(exc).__name__},
            )

    # 4. Single-instance guard for background work.
    owns_workers = settings.enable_background_workers or settings.enable_gateway
    is_leader = lock.acquire() if owns_workers else False
    app.state.is_worker_leader = is_leader

    # 7. Recover monitor state before the Gateway starts consuming events.
    if is_leader:
        try:
            await _recover_monitors(clients, database)
        except Exception:  # noqa: BLE001
            logger.exception("Monitor recovery failed; continuing startup")

    # 5. Gateway.
    if is_leader and settings.enable_gateway:
        await clients.start_gateway()
    elif not is_leader and owns_workers:
        logger.info(
            "This process does not hold the worker lock; serving HTTP only "
            "(no Gateway, no reconciliation worker)"
        )

    # 6. Reconciliation worker.
    worker: AccessReconciliationWorker | None = None
    if is_leader and settings.enable_background_workers:
        worker = AccessReconciliationWorker(
            settings=settings, database=database, client_manager=clients
        )
        await worker.start()
    app.state.worker = worker

    logger.info(
        "Startup complete",
        extra={
            "worker_leader": is_leader,
            "gateway_enabled": settings.enable_gateway,
            "workers_enabled": settings.enable_background_workers,
        },
    )

    try:
        yield
    finally:
        logger.info("Shutting down Discord collection service")
        if worker is not None:
            await worker.stop()
        await clients.close()
        await database.disconnect()
        lock.release()
        logger.info("Shutdown complete")


def create_app(
    settings: Settings | None = None,
    *,
    rest_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``rest_transport`` lets tests substitute the Discord API without patching globals.
    """

    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    register_secret(settings.bot_token)
    register_api_key_secrets(settings)

    app = FastAPI(
        title="Discord Collection Service",
        description=DESCRIPTION,
        version=SERVICE_VERSION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Service liveness and dependency checks."},
            {"name": "console", "description": "The browser console served at /ui."},
            {
                "name": "discord",
                "description": (
                    "Guild/channel discovery, the private-channel access workflow, "
                    "historical collection, search, monitoring and notifications."
                ),
            },
        ],
    )

    database = Database(settings)
    database.connect()
    clients = DiscordClientManager(settings, database, rest_transport=rest_transport)

    app.state.settings = settings
    app.state.database = database
    app.state.clients = clients
    app.state.worker = None
    app.state.worker_lock = ProcessLock(settings.worker_lock_file)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
        logger.info(
            "CORS enabled", extra={"origins": len(settings.cors_origin_list)}
        )

    app.include_router(health_router)
    app.include_router(discord_router)
    app.include_router(ui_router)
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate internal exceptions into sanitized JSON error responses."""

    @app.exception_handler(DiscordServiceError)
    async def _handle_service_error(
        request: Request, exc: DiscordServiceError
    ) -> JSONResponse:
        log = logger.warning if exc.http_status < 500 else logger.error
        log(
            "Request failed",
            extra={
                "path": request.url.path,
                "code": exc.code,
                "http_status": exc.http_status,
            },
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": {"errors": _sanitize_validation_errors(exc.errors())},
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback server-side; return nothing internal to the caller.
        logger.exception(
            "Unhandled error",
            extra={"path": request.url.path, "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred.",
                "details": {"timestamp": datetime.now(UTC).isoformat()},
            },
        )


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip inputs and exception objects out of Pydantic validation errors."""

    return [
        {
            "location": list(error.get("loc", [])),
            "message": str(error.get("msg", "invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in errors
    ]


app = create_app()
