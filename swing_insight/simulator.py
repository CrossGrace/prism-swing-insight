"""Point-in-time daily-bar replay for a 0–3-session KR swing hypothesis.

Research only: fills are modeled from subsequent OHLC, never broker executions.
The signal is deliberately named RSI reversal, *not* MSI (an ambiguous label).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from swing_insight.market import MarketDataUnavailable, evaluate_market, normalized_bars
from swing_insight.watchlist import WatchItem


@dataclass(frozen=True)
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ReplayPolicy:
    rsi_ceiling: float
    three_day_dip_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_days: int
    fee_bps: float
    sell_tax_bps: float
    slippage_bps: float

    def __post_init__(self) -> None:
        floats = (self.rsi_ceiling, self.three_day_dip_pct, self.stop_loss_pct,
                  self.take_profit_pct, self.fee_bps, self.sell_tax_bps, self.slippage_bps)
        if not all(math.isfinite(v) for v in floats):
            raise ValueError("Replay parameters must be finite")
        if (not 0 < self.rsi_ceiling < 100 or not 0 < self.three_day_dip_pct < 100
                or not 0 < self.stop_loss_pct < 100 or not 0 < self.take_profit_pct < 100
                or type(self.max_hold_days) is not int or not 0 <= self.max_hold_days <= 3
                or min(self.fee_bps, self.sell_tax_bps, self.slippage_bps) < 0
                or self.slippage_bps >= 10000):
            raise ValueError("Invalid replay policy; max_hold_days must be 0..3")


def _candles(payload: dict[str, Any], end: str) -> list[Candle]:
    # This also verifies volume, dates, metadata and absence of future bars.
    normalized = normalized_bars(payload, as_of=end)
    result = []
    for bar in normalized:
        row = payload[bar.date]
        try:
            o, h, l = (float(row.get(field, row.get(field.lower())))
                       for field in ("Open", "High", "Low"))
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailable("MISSING_OHLC_FOR_REPLAY") from exc
        if not all(math.isfinite(v) and v > 0 for v in (o, h, l)):
            raise MarketDataUnavailable("INVALID_OHLC_FOR_REPLAY")
        if l > min(o, bar.close) or h < max(o, bar.close) or l > h:
            raise MarketDataUnavailable("INCONSISTENT_OHLC_FOR_REPLAY")
        result.append(Candle(bar.date, o, h, l, bar.close, bar.volume))
    return result


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI (RMA smoothing), the common chart-indicator convention.

    Uses only closes available on the signal date. The older last-14-deltas
    simple-average variant is Cutler RSI and has different signals.
    """
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(change, 0) for change in changes[:period]) / period
    loss = sum(max(-change, 0) for change in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if gain == loss == 0:
        return 50.0
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _exit_fill(candle: Candle, buy_price: float, policy: ReplayPolicy, hold_days: int
               ) -> tuple[float, str] | None:
    stop = buy_price * (1 - policy.stop_loss_pct / 100)
    take = buy_price * (1 + policy.take_profit_pct / 100)
    # At an open gap, there is no guarantee of a fill at a resting stop.
    if candle.open <= stop:
        return candle.open, "STOP_GAP"
    if candle.open >= take:
        return take, "TAKE_GAP_CAPPED"
    # With only daily bars, a stop+take collision has unknown event order.
    # Choose the worse outcome; never assume the profitable event occurred first.
    if candle.low <= stop:
        return stop, "STOP_FIRST_OR_COLLISION"
    if candle.high >= take:
        return take, "TAKE"
    if hold_days == policy.max_hold_days:
        return candle.close, "TIME_EXIT"
    return None


def replay_ticker(*, item: WatchItem, stock: dict[str, Any], index: dict[str, Any] | None,
                  start: str, end: str, policy: ReplayPolicy,
                  price_only: bool = False, assume_uptrend: bool = False,
                  market_mode: str = "strict") -> dict[str, Any]:
    """Replay with T-close signals and strictly T+1-open entry.

    One position at a time per ticker. Results are per-trade observations, not
    a combined portfolio curve. A dated evidence source gates all earlier bars.
    """
    begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    if market_mode not in {"strict", "tactical"} or (assume_uptrend and market_mode != "strict"):
        raise ValueError("Invalid market mode or conflicting forced-uptrend assumption")
    if finish < begin:
        raise ValueError("Replay end precedes start")
    if not item.evidence and not price_only:
        raise ValueError("Replay needs dated fundamental evidence; no hindsight LTA")
    stock_bars = _candles(stock, end)
    index_bars = [] if assume_uptrend else normalized_bars(index, as_of=end)
    index_by_date = {b.date: b for b in index_bars}
    index_position = {b.date: i for i, b in enumerate(index_bars)}
    stock_dict = {b.date: {"Close": b.close, "Volume": b.volume} for b in stock_bars}
    index_dict = {b.date: {"Close": b.close, "Volume": b.volume} for b in index_bars}
    # The thesis contains *all* current source claims. Conservatively prevent
    # any of them from contaminating an earlier historical decision.
    eligible_from = (begin if price_only else max(
        begin, *(date.fromisoformat(e.as_of) for e in item.evidence)))
    trades = []
    open_position = None
    blocked_signals = []
    skip_until = -1
    for i in range(30, len(stock_bars) - 1):
        signal_bar = stock_bars[i]
        signal_day = date.fromisoformat(signal_bar.date)
        if i <= skip_until or signal_day < eligible_from or signal_day >= finish:
            continue
        entry = stock_bars[i + 1]
        if date.fromisoformat(entry.date) > finish or (
            not assume_uptrend and entry.date not in index_by_date
        ):
            continue
        closes = [b.close for b in stock_bars[:i + 1]]
        rsi = _rsi(closes)
        if (rsi is None or rsi > policy.rsi_ceiling
                or signal_bar.close <= stock_bars[i - 1].close
                or signal_bar.close / stock_bars[i - 3].close - 1 > -policy.three_day_dip_pct / 100):
            continue
        # Index must be a confirmed same-session bar; never use future prices.
        position_fraction = 1.0
        market_state = "ASSUMED_UPTREND" if assume_uptrend else "UNKNOWN"
        if not assume_uptrend:
            if signal_bar.date not in index_by_date:
                blocked_signals.append({"date": signal_bar.date, "reason": "MISSING_SAME_DAY_KOSPI"})
                continue
            stock_cutoff = {day: data for day, data in stock_dict.items() if day <= signal_bar.date}
            index_cutoff = {day: data for day, data in index_dict.items() if day <= signal_bar.date}
            observation = evaluate_market(ticker=item.ticker, as_of=signal_bar.date,
                                          stock=stock_cutoff, index=index_cutoff)
            market_state = observation.index_state
            if (observation.source_status != "VERIFIED_DAILY"
                    or observation.price_date != signal_bar.date
                    or observation.index_date != signal_bar.date):
                blocked_signals.append({"date": signal_bar.date,
                                        "reason": ",".join(observation.blocking_reasons) or "INVALID_MARKET_DATA"})
                continue
            if market_mode == "strict" and not observation.new_entries_allowed:
                blocked_signals.append({"date": signal_bar.date,
                                        "reason": ",".join(observation.blocking_reasons) or observation.index_state})
                continue
            if market_mode == "tactical":
                # PRISM's trend classifier is retained, but tactical RSI reversals
                # may trade corrections at a smaller equity allocation. Reject
                # exceptionally sharp index selloffs; these are deliberately
                # fixed research assumptions, not optimized against the replay.
                idx = index_position[signal_bar.date]
                if idx < 3:
                    blocked_signals.append({"date": signal_bar.date,
                                            "reason": "INSUFFICIENT_KOSPI_PANIC_HISTORY"})
                    continue
                index_change_1d = index_bars[idx].close / index_bars[idx - 1].close - 1
                index_change_3d = index_bars[idx].close / index_bars[idx - 3].close - 1
                if index_change_1d <= -0.04 or index_change_3d <= -0.08:
                    blocked_signals.append({"date": signal_bar.date,
                                            "reason": "KOSPI_PANIC_DAILY_4PCT_OR_3DAY_8PCT"})
                    continue
                position_fraction = {"UPTREND": 1.0, "UNDER_PRESSURE": 0.5,
                                     "CORRECTION": 0.25}.get(market_state, 0.0)
                if position_fraction == 0:
                    blocked_signals.append({"date": signal_bar.date,
                                            "reason": "UNKNOWN_PRISM_MARKET_STATE"})
                    continue
        buy = entry.open * (1 + policy.slippage_bps / 10000)
        entry_cash = buy * (1 + policy.fee_bps / 10000)
        sell_slippage = 1 - policy.slippage_bps / 10000
        sell_cost = 1 - (policy.fee_bps + policy.sell_tax_bps) / 10000
        position = {"signal_date": signal_bar.date, "entry_date": entry.date,
                    "market_state": market_state, "position_fraction_of_sleeve": position_fraction,
                    "rsi14_signal": round(rsi, 2), "entry_price": round(buy, 3)}
        last_exit_idx = min(i + 1 + policy.max_hold_days, len(stock_bars) - 1)
        for exit_idx in range(i + 1, last_exit_idx + 1):
            exit_bar = stock_bars[exit_idx]
            hold = exit_idx - i - 1
            outcome = _exit_fill(exit_bar, buy, policy, hold)
            if outcome is None:
                continue
            exit_reference, reason = outcome
            sell = exit_reference * sell_slippage
            trades.append({**position, "exit_date": exit_bar.date, "hold_days": hold,
                           "exit_price": round(sell, 3), "exit_reason": reason,
                           "net_return_pct": round((sell * sell_cost / entry_cash - 1) * 100, 4)})
            skip_until = exit_idx
            break
        else:
            # The entry happened, but the position has no exit in the observed window.
            mark = stock_bars[-1]
            open_position = {**position, "mark_date": mark.date, "mark_close": mark.close,
                             "marked_net_return_pct": round(
                                 (mark.close * sell_slippage * sell_cost / entry_cash - 1) * 100, 4)}
            break
    returns = [t["net_return_pct"] for t in trades]
    # Idle sleeve capital remains cash; tactical sizing must affect the
    # *portfolio* return, not just annotate a hypothetical full-size fill.
    multiplier = math.prod(1 + t["position_fraction_of_sleeve"] * t["net_return_pct"] / 100
                           for t in trades)
    marked_multiplier = multiplier
    if open_position is not None:
        marked_multiplier *= 1 + (open_position["position_fraction_of_sleeve"]
                                  * open_position["marked_net_return_pct"] / 100)
    window = [b for b in stock_bars if begin <= date.fromisoformat(b.date) <= finish]
    if not window:
        raise ValueError("No market sessions found in the requested replay window")
    return {"ticker": item.ticker, "company": item.company,
            "requested_start": start, "end": end, "eligible_from": eligible_from.isoformat(),
            "evidence_gate": ("DISABLED_PRICE_ONLY_NO_FUNDAMENTAL_FILTER" if price_only else
                              "NO_SIGNAL_BEFORE_LATEST_DATED_EVIDENCE"),
            "market_guard": ("ASSUMED_UPTREND_NOT_VERIFIED" if assume_uptrend else
                             "PRISM_MARKET_PULSE_TACTICAL_SIZED" if market_mode == "tactical" else
                             "PRISM_MARKET_PULSE_STRICT"),
            "tactical_risk_rule": ("SLEEVE_UPTREND_100_UNDER_PRESSURE_50_CORRECTION_25;"
                                   "KOSPI_PANIC_1DAY_MINUS4_3DAY_MINUS8"
                                   if market_mode == "tactical" else None),
            "signal": "RSI14_DIP_REVERSAL", "model": asdict(policy),
            "trades": trades, "count": len(trades), "open_position": open_position,
            "market_guard_rejections": blocked_signals,
            "win_rate_pct": round(100 * sum(r > 0 for r in returns) / len(returns), 2)
                            if returns else None,
            "mean_net_trade_pct": round(sum(returns) / len(returns), 3) if returns else None,
            "worst_net_trade_pct": min(returns) if returns else None,
            "compounded_net_sleeve_pct": round((multiplier - 1) * 100, 3),
            "marked_net_sleeve_pct": round((marked_multiplier - 1) * 100, 3),
            "buyhold_close_to_close_pct": round((window[-1].close / window[0].close - 1) * 100, 3),
            "buyhold_first_date": window[0].date,
            "buyhold_last_date": window[-1].date,
            "status": "COMPLETE_RESEARCH_REPLAY_NOT_REALIZED_RETURNS"}
