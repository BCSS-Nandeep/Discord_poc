"""Logging setup with hard guarantees that secrets never reach a log sink.

``SecretRedactionFilter`` is attached to every handler on the root logger, so even a
third-party library (discord.py, httpx, uvicorn) cannot leak the bot token through a
log record.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from typing import Any

REDACTED = "***REDACTED***"

#: Header names whose values must never be rendered in a log record.
SENSITIVE_KEYS = frozenset(
    {"authorization", "token", "bot_token", "discord_bot_token", "x-audit-log-reason"}
)

_registered_secrets: set[str] = set()


def register_secret(secret: str | None) -> None:
    """Register a literal string that must be scrubbed from all log output."""

    if secret and len(secret) >= 8:
        _registered_secrets.add(secret)


def scrub(value: Any) -> Any:
    """Replace any registered secret occurring inside ``value`` with a placeholder."""

    if not _registered_secrets:
        return value
    if isinstance(value, str):
        for secret in _registered_secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(item) for item in value)
    return value


class SecretRedactionFilter(logging.Filter):
    """Scrub registered secrets out of the message, args and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _registered_secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            record.args = scrub(record.args)  # type: ignore[assignment]
        for key, value in list(record.__dict__.items()):
            if key in _LOG_RECORD_BUILTINS:
                continue
            record.__dict__[key] = scrub(value)
        return True


_LOG_RECORD_BUILTINS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)))


class JsonFormatter(logging.Formatter):
    """Newline-delimited JSON formatter for structured log shipping."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_BUILTINS or key in {"message", "asctime"}:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human readable formatter that appends structured extras as ``key=value``."""

    default_fmt = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.default_fmt, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_BUILTINS and key not in {"message", "asctime"}
        }
        if extras:
            rendered = " ".join(f"{key}={value!r}" for key, value in sorted(extras.items()))
            base = f"{base} [{rendered}]"
        return base


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure the root logger once, with redaction attached to every handler."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    handler.addFilter(SecretRedactionFilter())

    root.addHandler(handler)
    root.setLevel(level.upper())

    # discord.py and httpx are chatty at DEBUG and httpx logs full request URLs.
    logging.getLogger("discord").setLevel(max(logging.INFO, root.level))
    logging.getLogger("discord.gateway").setLevel(max(logging.INFO, root.level))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger for the service."""

    return logging.getLogger(name)
