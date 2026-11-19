"""Separate CLI: research, audit and paper fills; no live-order command exists."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from swing_insight.analyst import analyze_thesis
from swing_insight.journal import PaperPolicy, SwingJournal
from swing_insight.market import evaluate_market
from swing_insight.simulator import ReplayPolicy, replay_ticker
from swing_insight.sources import get_financial_evidence, get_market_snapshots
from swing_insight.watchlist import load_watchlist


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated KR fundamental swing research / paper trading")
    parser.add_argument("--db", default="runtime/swing_insight.sqlite", help="Separate paper and thesis SQLite file")
    subs = parser.add_subparsers(dest="command", required=True)

    review = subs.add_parser("review", help="Read-only MCP snapshot, optional OpenAI thesis check")
    review.add_argument("--watchlist", required=True)
    review.add_argument("--as-of", help="Completed Korean trading date YYYY-MM-DD")
    review.add_argument("--model", help="OpenAI model; omit to collect risk facts without an LLM call")
    review.add_argument("--news", action="store_true", help="Add optional KIS headline context")
    review.add_argument("--fundamentals", action="store_true",
                        help="Use PRISM's current WiseReport actual financial statements")

    replay = subs.add_parser("simulate", help="0..3 day historical OHLC replay, next-open fill; NO broker")
    replay.add_argument("--watchlist", required=True)
    replay.add_argument("--start", default="2026-01-01")
    replay.add_argument("--end", required=True, help="Last confirmed session date; preferably D-1")
    replay.add_argument("--rsi-ceiling", type=float, required=True)
    replay.add_argument("--dip-pct", type=float, required=True,
                        help="Required negative three-session return at signal close")
    replay.add_argument("--stop-pct", type=float, required=True)
    replay.add_argument("--take-pct", type=float, required=True)
    replay.add_argument("--max-hold-days", type=int, required=True, help="0..3 trading sessions after entry")
    replay.add_argument("--fee-bps", type=float, required=True)
    replay.add_argument("--sell-tax-bps", type=float, required=True)
    replay.add_argument("--slippage-bps", type=float, required=True)

    setup = subs.add_parser("paper-init", help="One-time, explicit paper risk and cost budget")
    setup.add_argument("--capital", required=True, type=float)
    setup.add_argument("--single-limit", required=True, type=float,
                       help="Maximum fractional equity exposure to one ticker, e.g. 0.2")
    setup.add_argument("--total-limit", required=True, type=float)
    setup.add_argument("--fee-bps", required=True, type=float)
    setup.add_argument("--sell-tax-bps", required=True, type=float)

    fill = subs.add_parser("paper-fill", help="MANUAL hypothetical close fill (not a broker order)")
    fill.add_argument("--watchlist", required=True)
    fill.add_argument("--ticker", required=True)
    fill.add_argument("--side", required=True, choices=("BUY", "SELL"))
    fill.add_argument("--shares", required=True, type=int)
    fill.add_argument("--as-of", required=True)
    fill.add_argument("--request-id", required=True, help="Stable unique id for safe retries")

    subs.add_parser("status", help="Paper cash, holdings and last observed value")
    return parser


async def _run_review(args, journal: SwingJournal) -> list[dict]:
    as_of = args.as_of or date.today(ZoneInfo("Asia/Seoul")).isoformat()
    # Keep this independent of default PRISM scanner, report process and order loops.
    results = []
    for item in load_watchlist(args.watchlist):
        try:
            stock, index = await get_market_snapshots(item.ticker, as_of)
        except Exception:  # noqa: BLE001 - redact MCP/credential errors
            stock, index = {"error": "MCP_UNAVAILABLE"}, {"error": "MCP_UNAVAILABLE"}
        market = evaluate_market(ticker=item.ticker, as_of=as_of, stock=stock, index=index)
        news = ""
        if args.news:
            try:
                from prism_core.kr_news_context import fetch_context
                news = await fetch_context(item.company, market="KR")
            except Exception:  # noqa: BLE001 - optional headline evidence only
                pass
        financial_evidence = await get_financial_evidence(item.ticker, as_of) if args.fundamentals else ()
        opinion = await analyze_thesis(item, market, model=args.model, news_titles=news,
                                       additional_evidence=financial_evidence)
        results.append(journal.append_review(item, market, opinion, news_titles=news,
                                               additional_evidence=financial_evidence))
    return results


async def _run_simulate(args) -> list[dict]:
    from datetime import datetime, timedelta

    end = date.fromisoformat(args.end)
    last_safe = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
    if end > last_safe:
        raise ValueError("Backtest end must be before today (KST); intraday bars are unconfirmed")
    start = date.fromisoformat(args.start)
    policy = ReplayPolicy(args.rsi_ceiling, args.dip_pct, args.stop_pct,
                          args.take_pct, args.max_hold_days, args.fee_bps,
                          args.sell_tax_bps, args.slippage_bps)
    results = []
    for item in load_watchlist(args.watchlist):
        lookback = max(360, (end - start).days + 180)
        stock, index = await get_market_snapshots(item.ticker, args.end, history_days=lookback)
        results.append(replay_ticker(item=item, stock=stock, index=index,
                                     start=args.start, end=args.end, policy=policy))
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Pure historical research must not create or modify a paper account.
        if args.command == "simulate":
            result = asyncio.run(_run_simulate(args))
        elif args.command == "review":
            journal = SwingJournal(Path(args.db))
            result = asyncio.run(_run_review(args, journal))
        elif args.command == "paper-init":
            journal = SwingJournal(Path(args.db))
            journal.initialize_paper(PaperPolicy(
                args.capital, args.single_limit, args.total_limit,
                args.fee_bps, args.sell_tax_bps,
            ))
            result = {"status": "PAPER_ACCOUNT_INITIALIZED", "live_orders": False}
        elif args.command == "paper-fill":
            journal = SwingJournal(Path(args.db))
            result = journal.paper_fill(
                ticker=args.ticker, side=args.side, shares=args.shares,
                as_of=args.as_of, request_id=args.request_id,
                current_tickers={item.ticker for item in load_watchlist(args.watchlist)},
            )
        else:
            journal = SwingJournal(Path(args.db))
            result = journal.paper_snapshot()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        # Do not echo secrets, provider exception strings or private data.
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
