"""Keyword filtering shared by historical scraping, live monitoring and search."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

MatchMode = Literal["substring", "exact", "word"]

MAX_KEYWORDS = 100
MAX_KEYWORD_LENGTH = 200


@dataclass(slots=True)
class KeywordConfig:
    """How a monitor or scrape should filter messages.

    ``substring`` (the default) is a case-insensitive ``in`` test.  ``word`` matches
    whole words only, and ``exact`` requires the whole message to equal the keyword.
    """

    keywords: list[str] = field(default_factory=list)
    match_mode: MatchMode = "substring"
    case_sensitive: bool = False
    #: When false, only messages that matched at least one keyword are stored.
    store_non_matching: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.keywords)

    def to_json(self) -> str:
        return json.dumps(
            {
                "keywords": self.keywords,
                "match_mode": self.match_mode,
                "case_sensitive": self.case_sensitive,
                "store_non_matching": self.store_non_matching,
            }
        )

    @classmethod
    def from_json(cls, raw: str | None) -> KeywordConfig:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeywordConfig:
        keywords = data.get("keywords") or []
        if not isinstance(keywords, (list, tuple)):
            keywords = []
        mode = data.get("match_mode", "substring")
        if mode not in ("substring", "exact", "word"):
            mode = "substring"
        return cls(
            keywords=normalize_keywords(keywords),
            match_mode=mode,  # type: ignore[arg-type]
            case_sensitive=bool(data.get("case_sensitive", False)),
            store_non_matching=bool(data.get("store_non_matching", True)),
        )


def normalize_keywords(keywords: Sequence[str]) -> list[str]:
    """Trim, de-duplicate and bound a caller-supplied keyword list."""

    seen: set[str] = set()
    cleaned: list[str] = []
    for keyword in keywords:
        text = str(keyword).strip()
        if not text or len(text) > MAX_KEYWORD_LENGTH:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(text)
        if len(cleaned) >= MAX_KEYWORDS:
            break
    return cleaned


class KeywordMatcher:
    """Applies a :class:`KeywordConfig` to message text."""

    def __init__(self, config: KeywordConfig) -> None:
        self.config = config
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        if config.match_mode == "word" and config.keywords:
            flags = 0 if config.case_sensitive else re.IGNORECASE
            self._patterns = [
                (keyword, re.compile(rf"\b{re.escape(keyword)}\b", flags))
                for keyword in config.keywords
            ]

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def match(self, content: str | None) -> list[str]:
        """Return every keyword that matched ``content`` (empty when none did)."""

        if not self.config.enabled:
            return []
        text = content or ""
        if not text:
            return []

        if self.config.match_mode == "word":
            return [keyword for keyword, pattern in self._patterns if pattern.search(text)]

        haystack = text if self.config.case_sensitive else text.lower()
        matched: list[str] = []
        for keyword in self.config.keywords:
            needle = keyword if self.config.case_sensitive else keyword.lower()
            if self.config.match_mode == "exact":
                if haystack.strip() == needle:
                    matched.append(keyword)
            elif needle in haystack:
                matched.append(keyword)
        return matched

    def should_store(self, matched: Sequence[str]) -> bool:
        """Whether a message should be persisted given its match result."""

        if not self.config.enabled:
            return True
        if self.config.store_non_matching:
            return True
        return bool(matched)
