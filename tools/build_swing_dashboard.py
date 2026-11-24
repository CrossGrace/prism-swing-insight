"""Build the read-only daily swing research dashboard."""

import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swing_insight.simulator import ReplayPolicy, replay_ticker
from swing_insight.sources import get_public_research_kospi, get_public_research_ohlcv
from swing_insight.watchlist import load_watchlist

START = "2026-09-11"
END = "2026-11-20"
POLICY = ReplayPolicy(45, 2, 3, 5, 3, 10, 20, 10)


async def build():
    if date.fromisoformat(END) >= datetime.now(ZoneInfo("Asia/Seoul")).date():
        raise ValueError("Only completed prior sessions may be exported.")
    watchlist = load_watchlist(ROOT / "examples/swing_watchlist_oci_ls.json")
    index = await get_public_research_kospi(END)
    instruments = []
    for item in watchlist:
        stock = await get_public_research_ohlcv(item.ticker, END, start=START)
        if sorted(day for day in stock if START <= day <= END) != sorted(
            day for day in index if START <= day <= END
        ):
            raise ValueError(f"Incomplete stock or index coverage: {item.ticker}")
        variants = {}
        for mode in ("strict", "assumed"):
            variants[mode] = replay_ticker(
                item=item, stock=stock, index=index, start=START, end=END, policy=POLICY,
                price_only=True, assume_uptrend=mode == "assumed",
            )
        instruments.append({
            "ticker": item.ticker,
            "name": item.company,
            "prices": [
                {"date": day, "close": stock[day]["Close"]}
                for day in sorted(stock) if START <= day <= END
            ],
            "variants": variants,
        })

    output = ROOT / "dist/swing-dashboard"
    shutil.copytree(ROOT / "dashboard/swing", output, dirs_exist_ok=True)
    (output / "replay.json").write_text(json.dumps({
        "start": START, "end": END, "instruments": instruments,
    }, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(build())
