"""Service health endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import ClientsDep, DatabaseDep, SettingsDep
from app.core.exceptions import DiscordAPIError, DiscordNotConfiguredError
from app.core.logging import get_logger
from app.schemas.health import HealthResponse

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

SERVICE_VERSION = "1.0.0"


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    description=(
        "Liveness and dependency check for the Discord collection service. "
        "Never exposes the bot token or any other secret."
    ),
)
async def health(
    settings: SettingsDep, database: DatabaseDep, clients: ClientsDep
) -> HealthResponse:
    """Report database and Discord connectivity."""

    database_ok = await database.healthcheck()

    discord_state: str = "unconfigured"
    if settings.discord_configured:
        try:
            # A real probe, not the cached identity: a token revoked after startup
            # must show up here. This is one cheap GET /users/@me per health call.
            await clients.verify_bot(refresh=True)
            discord_state = "connected"
        except DiscordNotConfiguredError:
            discord_state = "unconfigured"
        except DiscordAPIError as exc:
            discord_state = "error"
            logger.warning(
                "Discord health check failed", extra={"status": exc.status_code}
            )
        except Exception:  # noqa: BLE001 - health must never raise
            discord_state = "disconnected"
            logger.exception("Unexpected error during Discord health check")

    if database_ok and discord_state == "connected":
        status = "ok"
    elif not database_ok:
        status = "error"
    else:
        status = "degraded"

    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=settings.app_name,
        version=SERVICE_VERSION,
        database="connected" if database_ok else "disconnected",
        discord=discord_state,  # type: ignore[arg-type]
        auth="enabled" if settings.auth_enabled else "disabled",
        timestamp=datetime.now(UTC),
    )
