"""Separate CLI: research, audit and paper fills; no live-order command exists."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from swing_insight.analyst import analyze_thesis
from swing_insight.blind import BlindPolicy, BlindStudy
from swing_insight.journal import PaperPolicy, SwingJournal
from swing_insight.market import evaluate_market
from swing_insight.simulator import ReplayPolicy, replay_ticker
from swing_insight.sources import (get_financial_evidence, get_market_snapshots,
                                   get_public_research_ohlcv, get_public_research_kospi)
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
    replay.add_argument("--source", choices=("prism-mcp", "public-research"), default="prism-mcp",
                        help="Public-research source is non-commercial, daily-only and no KIS key needed")
    replay.add_argument("--price-only", action="store_true",
                        help="Price-signal-only replay, explicitly ignoring currently known LTA facts")
    replay.add_argument("--assume-uptrend", action="store_true",
                        help="User scenario: bypass MarketPulse entirely, not a verified market state")
    replay.add_argument("--market-mode", choices=("strict", "tactical"), default="strict",
                        help="Tactical allows reduced-size contrarian entries in PRISM correction/pressure, with panic veto")

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

    blind_init = subs.add_parser("blind-init", help="Seal a FUTURE-only OCI paper experiment (no broker)")
    blind_init.add_argument("--start-date", required=True, help="First FUTURE Korean session YYYY-MM-DD")
    blind_init.add_argument("--capital", required=True, type=float)
    blind_init.add_argument("--stop-pct", required=True, type=float)
    blind_init.add_argument("--take-pct", required=True, type=float)
    blind_init.add_argument("--max-hold-days", required=True, type=int)
    blind_init.add_argument("--fee-bps", required=True, type=float)
    blind_init.add_argument("--sell-tax-bps", required=True, type=float)
    blind_init.add_argument("--slippage-bps", required=True, type=float)

    blind_step = subs.add_parser("blind-step", help="PRISM MCP after-close forward session; NO backfill")
    blind_step.add_argument("--as-of", required=True, help="Must be today's completed KST session")

    subs.add_parser("blind-status", help="Verify frozen policy and append-only forward paper events")
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


async def _run_simulate(args) -> dict:
    from datetime import datetime, timedelta

    end = date.fromisoformat(args.end)
    last_safe = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
    if end > last_safe:
        raise ValueError("Backtest end must be before today (KST); intraday bars are unconfirmed")
    start = date.fromisoformat(args.start)
    policy = ReplayPolicy(args.rsi_ceiling, args.dip_pct, args.stop_pct,
                          args.take_pct, args.max_hold_days, args.fee_bps,
                          args.sell_tax_bps, args.slippage_bps)
    if args.assume_uptrend and args.market_mode != "strict":
        raise ValueError("--assume-uptrend conflicts with --market-mode tactical")
    results = []
    public_index = None
    if args.source == "public-research" and not args.assume_uptrend:
        public_index = await get_public_research_kospi(args.end)
    for item in load_watchlist(args.watchlist):
        if args.source == "public-research":
            stock = await get_public_research_ohlcv(item.ticker, args.end, start=args.start)
            index = public_index
        else:
            lookback = max(360, (end - start).days + 180)
            stock, index = await get_market_snapshots(item.ticker, args.end, history_days=lookback)
        results.append(replay_ticker(item=item, stock=stock, index=index,
                                     start=args.start, end=args.end, policy=policy,
                                     price_only=args.price_only, assume_uptrend=args.assume_uptrend,
                                     market_mode=args.market_mode))
    return {
        "scenario": "RESEARCH_ONLY_NOT_REALIZED_PERFORMANCE",
        "data_source": args.source,
        "market_assumption": ("FORCED_UPTREND" if args.assume_uptrend else
                              "PRISM_MARKET_PULSE_TACTICAL_SIZED" if args.market_mode == "tactical"
                              else "PRISM_MARKET_PULSE_STRICT"),
        "universe_selection_bias": "TWO_STOCKS_CHOSEN_WITH_TODAYS_KNOWLEDGE",
        "equal_weight_sleeves": len(results),
        "equal_weight_compounded_paper_return_pct": round(
            sum(r["compounded_net_sleeve_pct"] for r in results) / len(results), 3),
        "equal_weight_buyhold_reference_pct": round(
            sum(r["buyhold_close_to_close_pct"] for r in results) / len(results), 3),
        "per_ticker": results,
    }


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
        elif args.command == "blind-init":
            result = BlindStudy(args.db).initialize(BlindPolicy(
                start_date=args.start_date, initial_cash=args.capital,
                stop_pct=args.stop_pct, take_pct=args.take_pct,
                max_hold_days=args.max_hold_days,
                fee_bps=args.fee_bps, sell_tax_bps=args.sell_tax_bps,
                slippage_bps=args.slippage_bps))
        elif args.command == "blind-step":
            result = asyncio.run(BlindStudy(args.db).step(args.as_of))
        elif args.command == "blind-status":
            result = BlindStudy(args.db).status()
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
