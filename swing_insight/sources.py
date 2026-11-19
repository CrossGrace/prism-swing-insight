"""Read-only access to the PRISM KIS-backed market-data MCP server."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from prism_core.report_research_prefetch import native_call
from swing_insight.watchlist import Evidence


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
