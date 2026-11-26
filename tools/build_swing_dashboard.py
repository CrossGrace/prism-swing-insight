"""Build the read-only daily swing research dashboard."""

import asyncio
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swing_insight.simulator import RecoveryPolicy, ReplayPolicy, replay_ticker
from swing_insight.sources import get_public_research_kospi, get_public_research_ohlcv
from swing_insight.watchlist import load_watchlist

START = "2026-09-11"
END = "2026-11-26"
LAST_VERIFIED_STOCK_DAY = "2026-11-20"
POLICY = ReplayPolicy(45, 2, 3, 5, 3, 10, 20, 10)


def get_naver_stock_tail(ticker: str, start: str, end: str) -> dict:
    queries = urlencode({
        "symbol": ticker, "timeframe": "day", "requestType": 0, "count": 60,
    })
    url = f"https://fchart.stock.naver.com/sise.nhn?{queries}"
    with urlopen(Request(url, headers={"User-Agent": "PRISM-swing-insight-research/0.1"}),
                 timeout=15) as response:
        payload = response.read()
    declaration = re.match(rb"""<\?xml[^>]*encoding=["']([a-zA-Z0-9_-]+)["']""", payload, re.IGNORECASE)
    encoding = declaration[1].decode("ascii") if declaration else "utf-8-sig"
    root = ElementTree.fromstring(payload.decode(encoding))
    chart = root if root.tag == "chartdata" else root.find(".//chartdata")
    if chart is None or chart.get("symbol", ticker) != ticker:
        raise ValueError(f"Naver returned no matching stock chart: {ticker}")
    rows = {}
    for item in chart.findall(".//item"):
        stamp, *values = item.attrib["data"].split("|")
        day = date.fromisoformat(stamp).isoformat()
        if not start <= day <= end:
            continue
        if day in rows:
            raise ValueError(f"Unexpected Naver trading date: {ticker} {day}")
        o, h, l, c, v = map(float, values)
        rows[day] = {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
    return rows


async def get_stock_for_replay(ticker: str) -> tuple[dict, str]:
    try:
        return await get_public_research_ohlcv(ticker, END, start=START), "한국주식데이터"
    except ValueError as exc:
        if str(exc) != "PUBLIC_DAILY_PRICE_NOT_YET_AVAILABLE_FOR_END_DATE":
            raise
    historical = await get_public_research_ohlcv(ticker, LAST_VERIFIED_STOCK_DAY, start=START)
    anchor = date.fromisoformat(LAST_VERIFIED_STOCK_DAY)
    overlap_start = (anchor - timedelta(days=10)).isoformat()
    tail = await asyncio.to_thread(get_naver_stock_tail, ticker, overlap_start, END)
    overlap = [day for day in historical if overlap_start <= day <= anchor.isoformat()]
    if LAST_VERIFIED_STOCK_DAY not in overlap or END not in tail or any(
        day not in tail or any(float(historical[day][key]) != tail[day][key]
                               for key in ("Open", "High", "Low", "Close"))
        for day in overlap
    ):
        raise ValueError(f"Naver daily price overlap or end date could not be verified: {ticker}")
    historical.update({day: row for day, row in tail.items() if day > LAST_VERIFIED_STOCK_DAY})
    return historical, "한국주식데이터 · 11/23 이후 네이버 금융"


async def build():
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    end_day = date.fromisoformat(END)
    if end_day > now.date() or (end_day == now.date() and now.time() < time(16, 10)):
        raise ValueError("The requested trading session must close before export.")
    watchlist = load_watchlist(ROOT / "examples/swing_watchlist_oci_ls.json")
    index = await get_public_research_kospi(END)
    instruments = []
    for item in watchlist:
        stock, price_source = await get_stock_for_replay(item.ticker)
        if END not in stock or END not in index or sorted(day for day in stock if START <= day <= END) != sorted(
            day for day in index if START <= day <= END
        ):
            raise ValueError(f"Incomplete stock or index coverage: {item.ticker}")
        variants = {
            label: {
                mode: replay_ticker(
                    item=item, stock=stock, index=index, start=START, end=END, policy=POLICY,
                    price_only=True, assume_uptrend=mode == "assumed", recovery=recovery,
                )
                for mode in ("strict", "assumed")
            }
            for label, recovery in (("baseline", None), ("recovery", RecoveryPolicy()),
                                    ("gap", RecoveryPolicy(gap_entry=True)))
        }
        instruments.append({
            "ticker": item.ticker,
            "name": item.company,
            "price_source": price_source,
            "prices": [
                {"date": day, "close": stock[day]["Close"]}
                for day in sorted(stock) if START <= day <= END
            ],
            "variants": variants["baseline"],
            "recovery_variants": variants["recovery"],
            "gap_variants": variants["gap"],
        })

    output = ROOT / "dist/swing-dashboard"
    shutil.copytree(ROOT / "dashboard/swing", output, dirs_exist_ok=True)
    (output / "replay.json").write_text(json.dumps({
        "start": START, "end": END, "instruments": instruments,
    }, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(build())
