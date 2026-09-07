"""Schemas for Discord login."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SessionUser(BaseModel):
    """The signed-in Discord user. Never carries a token."""

    discord_user_id: str = Field(description="Discord user snowflake.")
    username: str | None = None
    global_name: str | None = None
    avatar_url: str | None = None


class AuthStatusResponse(BaseModel):
    """Current authentication state, for a client to branch on."""

    authenticated: bool = Field(description="True when a valid session cookie is present.")
    oauth_enabled: bool = Field(
        description="Whether Discord login is configured on this server."
    )
    api_key_required: bool = Field(
        description="Whether X-API-Key is required for /discord/* endpoints."
    )
    user: SessionUser | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "authenticated": True,
                "oauth_enabled": True,
                "api_key_required": True,
                "user": {
                    "discord_user_id": "444456789012345678",
                    "username": "nandeep",
                    "global_name": "Nandeep",
                    "avatar_url": None,
                },
            }
        }
    )
