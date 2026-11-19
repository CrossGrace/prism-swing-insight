"""Deterministic, as-of market observations from PRISM's existing MCP tools."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from cores.market_pulse import DailyBar, MarketPulse


class MarketDataUnavailable(ValueError):
    """Bad, missing or stale market evidence must never become a buy permission."""


@dataclass(frozen=True)
class PriceBar:
    date: str
    close: float
    volume: float


@dataclass(frozen=True)
class MarketObservation:
    as_of: str
    ticker: str
    price_date: str | None
    index_date: str | None
    stock_close: float | None
    stock_change_20d_pct: float | None
    stock_ma20: float | None
    index_close: float | None
    index_state: str
    distribution_days: int | None
    new_entries_allowed: bool
    blocking_reasons: tuple[str, ...]
    source_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalized_bars(payload: dict[str, Any], *, as_of: str, min_bars: int = 26) -> list[PriceBar]:
    """Parse the PRISM get_*_ohlcv MCP payload without future bars or invented zeros."""
    try:
        cutoff = date.fromisoformat(as_of)
    except (TypeError, ValueError) as exc:
        raise MarketDataUnavailable("INVALID_AS_OF_DATE") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise MarketDataUnavailable("PROVIDER_UNAVAILABLE")
    meta = payload.get("__meta__") or {}
    if meta and not isinstance(meta, dict):
        raise MarketDataUnavailable("INVALID_METADATA")
    # Today's partial bar is unsafe as a completed daily swing signal.
    if str(meta.get("bar_status", "")).lower() in {"partial", "intraday", "unconfirmed"}:
        raise MarketDataUnavailable("UNCONFIRMED_BAR")

    bars: dict[str, PriceBar] = {}
    for key, row in payload.items():
        if key == "__meta__":
            continue
        try:
            day = date.fromisoformat(key)
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailable("INVALID_BAR_DATE") from exc
        if day > cutoff:
            continue  # as-of contract, including in research replays
        if not isinstance(row, dict):
            raise MarketDataUnavailable("INVALID_BAR_RECORD")
        try:
            close = float(row.get("Close", row.get("close")))
            volume = float(row.get("Volume", row.get("volume")))
        except (ValueError, TypeError) as exc:
            raise MarketDataUnavailable("MISSING_PRICE_OR_VOLUME") from exc
        if not math.isfinite(close) or close <= 0 or not math.isfinite(volume) or volume < 0:
            raise MarketDataUnavailable("INVALID_PRICE_OR_VOLUME")
        if key in bars:
            raise MarketDataUnavailable("DUPLICATE_BAR_DATE")
        bars[key] = PriceBar(date=key, close=close, volume=volume)
    ordered = sorted(bars.values(), key=lambda b: b.date)
    if len(ordered) < min_bars:
        raise MarketDataUnavailable("INSUFFICIENT_DAILY_HISTORY")
    # A 7-calendar-day ceiling tolerates weekends and most holiday closures.
    if (cutoff - date.fromisoformat(ordered[-1].date)).days > 7:
        raise MarketDataUnavailable("STALE_DAILY_HISTORY")
    return ordered


def evaluate_market(
    *, ticker: str, as_of: str, stock: dict[str, Any], index: dict[str, Any]
) -> MarketObservation:
    """Risk-off on a KOSPI correction, missing bars, or an unresolved market state.

    This *only* advises whether a fresh simulated entry is eligible. It neither
    generates a buy nor changes the upstream PRISM buy/sell path.
    """
    reasons: list[str] = []
    stock_bars: list[PriceBar] = []
    index_bars: list[PriceBar] = []
    try:
        stock_bars = normalized_bars(stock, as_of=as_of)
    except MarketDataUnavailable as exc:
        reasons.append("STOCK_" + str(exc))
    try:
        index_bars = normalized_bars(index, as_of=as_of)
    except MarketDataUnavailable as exc:
        reasons.append("KOSPI_" + str(exc))

    pulse = MarketPulse()
    if index_bars:
        for bar in index_bars:
            pulse.feed(DailyBar(date=bar.date, close=bar.close, volume=bar.volume))
        if pulse.state == "CORRECTION":
            reasons.append("MARKET_CORRECTION")
        elif pulse.state == "UNDER_PRESSURE":
            reasons.append("MARKET_UNDER_PRESSURE_REVIEW")

    if stock_bars and stock_bars[-1].date != as_of:
        reasons.append("STOCK_NO_CONFIRMED_BAR_AS_OF")
    if index_bars and index_bars[-1].date != as_of:
        reasons.append("KOSPI_NO_CONFIRMED_BAR_AS_OF")
    # Correction and under-pressure are paper-entry vetoes, not sell triggers.
    can_enter = bool(stock_bars and index_bars and pulse.state == "UPTREND" and not reasons)
    return MarketObservation(
        as_of=as_of,
        ticker=ticker,
        price_date=stock_bars[-1].date if stock_bars else None,
        index_date=index_bars[-1].date if index_bars else None,
        stock_close=stock_bars[-1].close if stock_bars else None,
        stock_change_20d_pct=(round((stock_bars[-1].close / stock_bars[-21].close - 1) * 100, 3)
                               if len(stock_bars) >= 21 else None),
        stock_ma20=(round(sum(x.close for x in stock_bars[-20:]) / 20, 4)
                    if len(stock_bars) >= 20 else None),
        index_close=index_bars[-1].close if index_bars else None,
        index_state=pulse.state if index_bars else "UNKNOWN",
        distribution_days=pulse.distribution_days if index_bars else None,
        new_entries_allowed=can_enter,
        blocking_reasons=tuple(reasons),
        source_status="VERIFIED_DAILY" if stock_bars and index_bars else "MISSING",
    )
