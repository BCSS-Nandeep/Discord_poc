"""Shared schema building blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

T = TypeVar("T")

#: A Discord snowflake id. Validated at the edge so no unchecked value reaches a URL.
Snowflake = Annotated[
    str,
    StringConstraints(pattern=r"^\d{1,20}$", strip_whitespace=True),
    Field(description="Discord snowflake id (numeric string).", examples=["123456789012345678"]),
]

OptionalSnowflake = Annotated[
    str | None,
    StringConstraints(pattern=r"^\d{1,20}$", strip_whitespace=True),
]


class ORMModel(BaseModel):
    """Base for response models read directly from SQLAlchemy entities."""

    model_config = ConfigDict(from_attributes=True)


class Pagination(BaseModel):
    """Pagination envelope returned with list endpoints."""

    total: int = Field(description="Total rows matching the query.", ge=0)
    limit: int = Field(description="Maximum rows returned.", ge=1)
    offset: int = Field(description="Rows skipped.", ge=0)
    returned: int = Field(description="Rows in this response.", ge=0)


class Page(BaseModel, Generic[T]):
    """A page of results."""

    items: list[T]
    pagination: Pagination


class ErrorResponse(BaseModel):
    """Sanitized error body. Never contains secrets or internal stack traces."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human readable description.")
    details: dict | None = Field(default=None, description="Optional context.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "ACCESS_NOT_GRANTED",
                "message": "The bot cannot collect from this channel yet",
                "details": {
                    "channel_id": "123456789012345678",
                    "reason": "BOT_CANNOT_VIEW_CHANNEL",
                },
            }
        }
    )


class OperationResponse(BaseModel):
    """Simple acknowledgement body."""

    success: bool = True
    message: str
    timestamp: datetime
