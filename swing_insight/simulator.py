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


@dataclass(frozen=True)
class RecoveryPolicy:
    wait_sessions: int = 2
    first_sale_fraction: float = 0.5
    trail_pct: float = 5.0
    gap_entry: bool = False

    def __post_init__(self) -> None:
        if (type(self.wait_sessions) is not int or not 1 <= self.wait_sessions <= 2
                or not 0 < self.first_sale_fraction < 1 or not 0 < self.trail_pct < 100):
            raise ValueError("Invalid recovery research policy")


def _recovery_sales_today(bar: Candle, hold: int, buy: float, entry_at_open: bool,
                          stop: float, remaining: float, policy: ReplayPolicy,
                          recovery: RecoveryPolicy
                          ) -> tuple[list[tuple[float, str, float]], float]:
    """Process one observed bar using the stop known before that session."""
    hard_stop = buy * (1 - policy.stop_loss_pct / 100)
    target = buy * (1 + policy.take_profit_pct / 100)
    if hold and bar.open <= stop:
        return [(bar.open, "TRAIL_GAP" if stop > hard_stop else "STOP_GAP", remaining)], stop
    sales = []
    if hold and remaining == 1 and bar.open >= target:
        sales.append((target, "PARTIAL_TAKE", recovery.first_sale_fraction))
        remaining -= recovery.first_sale_fraction
    if bar.low <= stop:
        if hold == 0 and not entry_at_open:
            reason = "ENTRY_BAR_STOP_ASSUMED"
        elif remaining == 1 and bar.high >= target:
            reason = "STOP_COLLISION"
        else:
            reason = "TRAIL_STOP" if stop > hard_stop else "STOP"
        sales.append((stop, reason, remaining))
        return sales, stop
    if remaining == 1 and bar.high >= target:
        sales.append((target, "PARTIAL_TAKE", recovery.first_sale_fraction))
        remaining -= recovery.first_sale_fraction
    if hold == policy.max_hold_days:
        sales.append((bar.close, "TIME_EXIT", remaining))
    elif remaining < 1:
        stop = max(stop, bar.close * (1 - recovery.trail_pct / 100))
    return sales, stop


@dataclass(frozen=True)
class PendingEntry:
    signal_idx: int
    signal_date: str
    market_state: str
    position_fraction: float
    rsi: float


def _entry_today(pending: list[PendingEntry], i: int, bar: Candle, previous: Candle,
                 policy: ReplayPolicy, recovery: RecoveryPolicy | None
                 ) -> tuple[PendingEntry, float, str] | None:
    if not pending:
        return None
    first = pending[0]
    if recovery is None:
        return first, bar.open, "NEXT_OPEN"
    newest = pending[-1]
    if (recovery.gap_entry and newest.signal_idx == i - 1
            and bar.open <= previous.close * (1 - policy.three_day_dip_pct / 100)):
        return newest, bar.open, "RECOVERY_GAP"
    if bar.open >= previous.high:
        return first, bar.open, "RECOVERY_OPEN"
    if bar.high >= previous.high:
        return first, previous.high, "RECOVERY_BREAKOUT"
    return None


def replay_ticker(*, item: WatchItem, stock: dict[str, Any], index: dict[str, Any] | None,
                  start: str, end: str, policy: ReplayPolicy,
                  price_only: bool = False, assume_uptrend: bool = False,
                  market_mode: str = "strict",
                  recovery: RecoveryPolicy | None = None) -> dict[str, Any]:
    """Replay T-close signals with next-open or optional recovery entry.

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
    pending: list[PendingEntry] = []
    position = None
    entry_idx = -1
    buy = stop = realized = 0.0
    remaining = 0.0
    exits: list[dict[str, Any]] = []
    closes = [bar.close for bar in stock_bars[:30]]
    sell_slippage = 1 - policy.slippage_bps / 10000
    sell_cost = 1 - (policy.fee_bps + policy.sell_tax_bps) / 10000
    buy_fee = 1 + policy.fee_bps / 10000
    wait = recovery.wait_sessions if recovery else 1
    for i in range(30, len(stock_bars)):
        signal_bar = stock_bars[i]
        signal_day = date.fromisoformat(signal_bar.date)
        previous = stock_bars[i - 1]
        closes.append(signal_bar.close)
        pending = [signal for signal in pending if i - signal.signal_idx <= wait]
        if position is None and (assume_uptrend or signal_bar.date in index_by_date):
            entry = _entry_today(pending, i, signal_bar, previous, policy, recovery)
            if entry is not None:
                signal, reference, basis = entry
                buy = reference * (1 + policy.slippage_bps / 10000)
                stop = buy * (1 - policy.stop_loss_pct / 100)
                entry_idx = i
                remaining = 1.0
                realized = 0.0
                exits = []
                position = {"signal_date": signal.signal_date, "entry_date": signal_bar.date,
                            "market_state": signal.market_state,
                            "position_fraction_of_sleeve": signal.position_fraction,
                            "rsi14_signal": round(signal.rsi, 2), "entry_price": round(buy, 3)}
                if recovery:
                    position["entry_basis"] = basis
                pending.clear()
        if position is not None:
            hold = i - entry_idx
            if recovery:
                sales, stop = _recovery_sales_today(
                    signal_bar, hold, buy, position["entry_basis"] != "RECOVERY_BREAKOUT",
                    stop, remaining, policy, recovery)
            else:
                outcome = _exit_fill(signal_bar, buy, policy, hold)
                sales = [(*outcome, 1.0)] if outcome else []
            for reference, reason, fraction in sales:
                sell = reference * sell_slippage
                net = (sell * sell_cost / (buy * buy_fee) - 1) * 100
                realized += fraction * net
                remaining -= fraction
                exits.append({"exit_date": signal_bar.date, "exit_price": round(sell, 3),
                              "exit_reason": reason, "fraction": fraction, "net_return_pct": round(net, 4)})
            if remaining <= 0:
                final = {key: exits[-1][key] for key in ("exit_date", "exit_price", "exit_reason")}
                trade = {**position, **final, "hold_days": hold, "net_return_pct": round(realized, 4)}
                if recovery:
                    trade["exit_legs"] = exits
                trades.append(trade)
                position = None
            continue
        if signal_day < eligible_from:
            continue
        rsi = _rsi(closes)
        change = signal_bar.close - stock_bars[i - 1].close
        if (rsi is None or rsi > policy.rsi_ceiling
                or (change >= 0 if recovery else change <= 0)
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
        pending.append(PendingEntry(i, signal_bar.date, market_state, position_fraction, rsi))
    if position is not None:
        mark = stock_bars[-1]
        mark_net = (mark.close * sell_slippage * sell_cost / (buy * buy_fee) - 1) * 100
        open_position = {**position, "mark_date": mark.date, "mark_close": mark.close,
                         "marked_net_return_pct": round(realized + remaining * mark_net, 4)}
        if recovery:
            open_position.update(exit_legs=exits, remaining_fraction=remaining)
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
            "signal": "RSI14_DIP_RECOVERY" if recovery else "RSI14_DIP_REVERSAL",
            "execution": "BLIND_CHRONOLOGICAL_DAILY_REPLAY",
            "model": asdict(policy), "recovery": asdict(recovery) if recovery else None,
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
