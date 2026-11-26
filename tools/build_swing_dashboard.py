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
POLICY = ReplayPolicy(45, 2, 3, 5, 3, 10, 20, 10)


async def build():
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    end_day = date.fromisoformat(END)
    if end_day > now.date() or (end_day == now.date() and now.time() < time(16, 10)):
        raise ValueError("The requested trading session must close before export.")
    watchlist = load_watchlist(ROOT / "examples/swing_watchlist_oci_ls.json")
    index = await get_public_research_kospi(END)
    instruments = []
    for item in watchlist:
        stock = await get_public_research_ohlcv(item.ticker, END, start=START)
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
