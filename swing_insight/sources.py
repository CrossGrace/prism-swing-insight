"""Read-only access to the PRISM KIS-backed market-data MCP server."""

from __future__ import annotations

import asyncio
import json
import math
from urllib.request import Request, urlopen
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from prism_core.report_research_prefetch import native_call
from swing_insight.watchlist import Evidence


async def get_public_research_ohlcv(ticker: str, as_of: str, *, start: str) -> dict[str, Any]:
    """Historical research-only fallback from a public FSC daily-price mirror.

    The provider explicitly permits attributed non-commercial use, but forbids
    commercial redistribution. Keep the dataset outside git and live trading.
    Dates are calendar-checked; no hypothetical or incomplete last bar is added.
    https://aikstockdata.com/licenses/aiksd-public-1.1.txt
    """
    if ticker not in {"010060", "010120"}:
        raise ValueError("The public replay adapter is limited to the approved two tickers")

    def fetch() -> dict[str, Any]:
        url = f"https://aikstockdata.com/data/public/s/{ticker}_history.json"
        request = Request(url, headers={"User-Agent": "PRISM-swing-insight-research/0.1"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    try:
        raw = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001 - fail closed without propagating provider diagnostics
        raise ValueError("PUBLIC_DAILY_PRICE_SOURCE_UNAVAILABLE") from exc
    if not isinstance(raw, dict) or raw.get("code") != ticker or raw.get("columns") != [
        "date", "close", "volume", "open", "high", "low"
    ] or not isinstance(raw.get("rows"), list):
        raise ValueError("PUBLIC_DAILY_PRICE_SCHEMA_CHANGED")
    if raw.get("as_of", "") < as_of.replace("-", ""):
        raise ValueError("PUBLIC_DAILY_PRICE_NOT_YET_AVAILABLE_FOR_END_DATE")
    warmup_start = date.fromisoformat(start) - timedelta(days=90)
    for action in raw.get("breaks", []):
        if not isinstance(action, dict) or not isinstance(action.get("date"), str):
            raise ValueError("PUBLIC_DAILY_PRICE_CORPORATE_ACTION_UNKNOWN")
        action_date = date.fromisoformat(f"{action['date'][:4]}-{action['date'][4:6]}-{action['date'][6:]}")
        if warmup_start <= action_date <= date.fromisoformat(as_of):
            raise ValueError("PUBLIC_DAILY_PRICE_CORPORATE_ACTION_NEEDS_ADJUSTMENT")
    result: dict[str, Any] = {}
    for row in raw["rows"]:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("PUBLIC_DAILY_PRICE_ROW_INVALID")
        d, c, v, o, h, l = row
        if not isinstance(d, str) or len(d) != 8 or not d.isdigit():
            raise ValueError("PUBLIC_DAILY_PRICE_DATE_INVALID")
        day = date.fromisoformat(f"{d[:4]}-{d[4:6]}-{d[6:]}")
        if day < warmup_start or day > date.fromisoformat(as_of):
            continue
        if day.isoformat() in result:
            raise ValueError("PUBLIC_DAILY_PRICE_DUPLICATE")
        result[day.isoformat()] = {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
    if not result:
        raise ValueError("PUBLIC_DAILY_PRICE_EMPTY")
    return result


async def get_public_research_kospi(as_of: str) -> dict[str, Any]:
    """Provide real daily KOSPI close/volume to PRISM's MarketPulse in RESEARCH.

    Naver's published dayCandle history is finite (~110 trading sessions).
    Never extend it with invented bars/volume; the MarketPulse consumer decides
    whether a signal day's same-date index confirmation is available.
    """
    def fetch() -> dict[str, Any]:
        request = Request(
            "https://api.stock.naver.com/chart/domestic/index/KOSPI?periodType=dayCandle",
            headers={"User-Agent": "PRISM-swing-insight-research/0.1"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    try:
        raw = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("PUBLIC_KOSPI_DAILY_SOURCE_UNAVAILABLE") from exc
    if (not isinstance(raw, dict) or raw.get("code") != "KOSPI"
            or raw.get("periodType") != "dayCandle"
            or raw.get("hasVolume") is not True
            or not isinstance(raw.get("priceInfos"), list)):
        raise ValueError("PUBLIC_KOSPI_SCHEMA_CHANGED")
    data: dict[str, Any] = {}
    for row in raw["priceInfos"]:
        if not isinstance(row, dict):
            raise ValueError("PUBLIC_KOSPI_INVALID_ROW")
        day = row.get("localDate")
        if not isinstance(day, str) or len(day) != 8 or not day.isdigit():
            raise ValueError("PUBLIC_KOSPI_INVALID_DATE")
        day = date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}").isoformat()
        if day > as_of:
            continue
        if day in data:
            raise ValueError("PUBLIC_KOSPI_DUPLICATE_DATE")
        data[day] = {"Open": row.get("openPrice"), "High": row.get("highPrice"),
                     "Low": row.get("lowPrice"), "Close": row.get("closePrice"),
                     "Volume": row.get("accumulatedTradingVolume")}
    if as_of not in data:
        raise ValueError("PUBLIC_KOSPI_NO_CONFIRMED_BAR_AT_END_DATE")
    return data


async def get_market_snapshots(ticker: str, as_of: str, *, history_days: int = 360) -> tuple[
    dict[str, Any], dict[str, Any]
]:
    """Fetch stock and KOSPI daily bars using exactly PRISM's registered MCP tool names.

    The shared native_call function handles stdio lifecycle, source registry,
    secret isolation and MCP response decoding. No broker order API is used.
    """
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=history_days)
    params = {"fromdate": start.strftime("%Y%m%d"), "todate": end.strftime("%Y%m%d")}
    stock = await native_call("kospi_kosdaq", "get_stock_ohlcv", {
        **params, "ticker": ticker, "adjusted": True,
    })
    index = await native_call("kospi_kosdaq", "get_index_ohlcv", {
        **params, "ticker": "1001", "freq": "d",
    })
    return stock, index


async def get_financial_evidence(ticker: str, as_of: str) -> tuple[Evidence, ...]:
    """Reuse PRISM's bounded WiseReport financial parser, not a second scraper.

    WiseReport is a secondary source; never confuse its consensus estimates with
    reported actuals. Historical backfills are intentionally unsupported because
    the upstream webpage shows *today's* numbers, not an as-of historical view.
    """
    if date.fromisoformat(as_of) != datetime.now(ZoneInfo("Asia/Seoul")).date():
        return ()
    try:
        from prism_core.kr_financial_summary import (PAGE_URL,
            collect_wisereport_financial_summary)
        frame = await collect_wisereport_financial_summary(ticker, as_of.replace("-", ""))
    except Exception:  # noqa: BLE001 - optional provider must not authorize an entry
        return ()
    if frame is None or frame.empty or "estimate" not in frame.columns:
        return ()
    actual = frame.loc[~frame["estimate"].astype(bool)].tail(3)
    rows = []
    for fiscal_period, row in actual.iterrows():
        attributes = []
        for key, label in (("revenue", "매출액 억원"), ("operating_income", "영업이익 억원"),
                           ("op_margin", "영업이익률 %"), ("roe", "ROE %"),
                           ("debt_ratio", "부채비율 %")):
            value = row.get(key)
            try:
                numeric = float(value)
            except (ValueError, TypeError):
                continue
            if math.isfinite(numeric):
                attributes.append(f"{label}={numeric:g}")
        if attributes:
            rows.append(Evidence(
                id="wisereport-" + str(fiscal_period).replace("/", "-"),
                source="WiseReport annual actual (secondary; not independently audited here)",
                as_of=as_of,
                fact=f"회계연도 {fiscal_period} 발표값: " + ", ".join(attributes),
                url=PAGE_URL.format(code=ticker),
            ))
    return tuple(rows)
