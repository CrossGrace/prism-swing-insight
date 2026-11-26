"""Build the read-only daily swing research dashboard."""

import asyncio
from datetime import date, datetime, time
import json
from pathlib import Path
import shutil
import sys
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


# KRX regular-session OHLCV for the four sessions after the verified snapshot.
# OCI: https://kr.investing.com/equities/oci-co-ltd-historical-data
# Volume: https://stock.mk.co.kr/price/foreigner/KR7010060002
# LS: https://aikstockdata.com/s/010120 and https://markets.hankyung.com/stock/010120
# LS 11/26 open: https://kr.investing.com/equities/ls-industrial-systems-historical-data
VERIFIED_TAIL = {
    "010060": (
        ("2026-11-23", 152800, 155300, 143300, 149500, 261590),
        ("2026-11-24", 155400, 167300, 154900, 163800, 238592),
        ("2026-11-25", 157200, 161500, 153400, 156700, 116202),
        ("2026-11-26", 166800, 179700, 163400, 165400, 257972),
    ),
    "010120": (
        ("2026-11-23", 105000, 112500, 105000, 110200, 600647),
        ("2026-11-24", 116400, 123600, 116300, 122000, 685897),
        ("2026-11-25", 115800, 116900, 110800, 114300, 486864),
        ("2026-11-26", 115100, 117300, 112100, 113400, 366194),
    ),
}


async def get_stock_for_replay(ticker: str) -> tuple[dict, str]:
    try:
        return await get_public_research_ohlcv(ticker, END, start=START), "한국주식데이터"
    except ValueError as exc:
        if str(exc) != "PUBLIC_DAILY_PRICE_NOT_YET_AVAILABLE_FOR_END_DATE":
            raise
    historical = await get_public_research_ohlcv(ticker, LAST_VERIFIED_STOCK_DAY, start=START)
    tail = {
        day: {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
        for day, o, h, l, c, v in VERIFIED_TAIL[ticker]
        if LAST_VERIFIED_STOCK_DAY < day <= END
    }
    if LAST_VERIFIED_STOCK_DAY not in historical or END not in tail:
        raise ValueError(f"Verified KRX snapshot cannot cover the requested end date: {ticker}")
    return {**historical, **tail}, "한국주식데이터 · 11/23~11/26 장마감 시세 대조"


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
