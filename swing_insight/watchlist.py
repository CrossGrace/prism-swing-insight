"""Explicit human-curated universe and thesis; no auto-stock selection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_KR_TICKER = re.compile(r"^[0-9]{6}$")


@dataclass(frozen=True)
class Evidence:
    id: str
    source: str
    as_of: str
    fact: str
    url: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Evidence":
        keys = ("id", "source", "as_of", "fact", "url")
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k].strip()
                                                  for k in keys):
            raise ValueError("Each evidence item needs id, source, as_of, fact, url")
        date.fromisoformat(value["as_of"])
        if not value["url"].startswith("https://"):
            raise ValueError("Evidence must point to an attributable HTTPS source")
        return cls(**{k: value[k].strip() for k in keys})


@dataclass(frozen=True)
class WatchItem:
    ticker: str
    company: str
    thesis: str
    invalidation: tuple[str, ...]
    evidence: tuple[Evidence, ...]


def load_watchlist(path: str | Path) -> tuple[WatchItem, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    records = raw.get("watchlist") if isinstance(raw, dict) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 2:
        raise ValueError("The manual KR swing watchlist must have 1 or 2 stocks")
    result = []
    for row in records:
        if not isinstance(row, dict) or not _KR_TICKER.fullmatch(str(row.get("ticker", ""))):
            raise ValueError("Stock ticker must be an exact 6-digit KR stock code")
        if not isinstance(row.get("company"), str) or not row["company"].strip():
            raise ValueError("company is required")
        if not isinstance(row.get("thesis"), str) or not row["thesis"].strip():
            raise ValueError("A user-authored investment thesis is required")
        invalidation = row.get("invalidation")
        if not isinstance(invalidation, list) or not invalidation or not all(
            isinstance(s, str) and s.strip() for s in invalidation
        ):
            raise ValueError("Define at least one explicit thesis invalidation condition")
        evidence = tuple(Evidence.from_dict(v) for v in row.get("evidence", []))
        if len({item.id for item in evidence}) != len(evidence):
            raise ValueError("Evidence IDs must be unique per stock")
        result.append(WatchItem(row["ticker"], row["company"].strip(),
                                row["thesis"].strip(), tuple(invalidation), evidence))
    if len({item.ticker for item in result}) != len(result):
        raise ValueError("Duplicate stock in watchlist")
    return tuple(result)
