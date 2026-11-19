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
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gain = sum(max(change, 0) for change in changes) / period
    loss = sum(max(-change, 0) for change in changes) / period
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


def replay_ticker(*, item: WatchItem, stock: dict[str, Any], index: dict[str, Any],
                  start: str, end: str, policy: ReplayPolicy) -> dict[str, Any]:
    """Replay with T-close signals and strictly T+1-open entry.

    One position at a time per ticker. Results are per-trade observations, not
    a combined portfolio curve. A dated evidence source gates all earlier bars.
    """
    begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    if finish < begin:
        raise ValueError("Replay end precedes start")
    if not item.evidence:
        raise ValueError("Replay needs dated fundamental evidence; no hindsight LTA")
    stock_bars = _candles(stock, end)
    index_bars = normalized_bars(index, as_of=end)
    index_by_date = {b.date: b for b in index_bars}
    stock_dict = {b.date: {"Close": b.close, "Volume": b.volume} for b in stock_bars}
    index_dict = {b.date: {"Close": b.close, "Volume": b.volume} for b in index_bars}
    # The thesis contains *all* current source claims. Conservatively prevent
    # any of them from contaminating an earlier historical decision.
    eligible_from = max(begin, *(date.fromisoformat(e.as_of) for e in item.evidence))
    trades = []
    blocked_signals = []
    skip_until = -1
    for i in range(30, len(stock_bars) - 1):
        signal_bar = stock_bars[i]
        signal_day = date.fromisoformat(signal_bar.date)
        if i <= skip_until or signal_day < eligible_from or signal_day >= finish:
            continue
        entry = stock_bars[i + 1]
        if date.fromisoformat(entry.date) > finish or entry.date not in index_by_date:
            continue
        closes = [b.close for b in stock_bars[:i + 1]]
        rsi = _rsi(closes)
        if (rsi is None or rsi > policy.rsi_ceiling
                or signal_bar.close <= stock_bars[i - 1].close
                or signal_bar.close / stock_bars[i - 3].close - 1 > -policy.three_day_dip_pct / 100):
            continue
        # Index must be a confirmed same-session bar; never use future prices.
        if signal_bar.date not in index_by_date:
            blocked_signals.append({"date": signal_bar.date, "reason": "MISSING_SAME_DAY_KOSPI"})
            continue
        stock_cutoff = {day: data for day, data in stock_dict.items() if day <= signal_bar.date}
        index_cutoff = {day: data for day, data in index_dict.items() if day <= signal_bar.date}
        observation = evaluate_market(ticker=item.ticker, as_of=signal_bar.date,
                                      stock=stock_cutoff, index=index_cutoff)
        if not observation.new_entries_allowed:
            blocked_signals.append({"date": signal_bar.date,
                                    "reason": ",".join(observation.blocking_reasons) or observation.index_state})
            continue
        buy = entry.open * (1 + policy.slippage_bps / 10000)
        for hold in range(policy.max_hold_days + 1):
            exit_idx = i + 1 + hold
            if exit_idx >= len(stock_bars):
                break
            exit_bar = stock_bars[exit_idx]
            if date.fromisoformat(exit_bar.date) > finish:
                break
            outcome = _exit_fill(exit_bar, buy, policy, hold)
            if outcome is None:
                continue
            exit_reference, reason = outcome
            sell = exit_reference * (1 - policy.slippage_bps / 10000)
            entry_cash = buy * (1 + policy.fee_bps / 10000)
            exit_cash = sell * (1 - (policy.fee_bps + policy.sell_tax_bps) / 10000)
            trades.append({"signal_date": signal_bar.date, "entry_date": entry.date,
                           "exit_date": exit_bar.date, "hold_days": hold,
                           "rsi14_signal": round(rsi, 2), "entry_price": round(buy, 3),
                           "exit_price": round(sell, 3), "exit_reason": reason,
                           "net_return_pct": round((exit_cash / entry_cash - 1) * 100, 4)})
            skip_until = exit_idx
            break
        else:
            # A position whose full holding window extends past available
            # data is open; never invent the future close or count a win.
            skip_until = len(stock_bars)
    returns = [t["net_return_pct"] for t in trades]
    return {"ticker": item.ticker, "company": item.company,
            "requested_start": start, "end": end, "eligible_from": eligible_from.isoformat(),
            "evidence_gate": "NO_SIGNAL_BEFORE_LATEST_DATED_EVIDENCE",
            "signal": "RSI14_DIP_REVERSAL_NOT_MSI", "model": asdict(policy),
            "trades": trades, "count": len(trades),
            "market_guard_rejections": blocked_signals,
            "win_rate_pct": round(100 * sum(r > 0 for r in returns) / len(returns), 2)
                            if returns else None,
            "mean_net_trade_pct": round(sum(returns) / len(returns), 3) if returns else None,
            "worst_net_trade_pct": min(returns) if returns else None,
            "status": "COMPLETE_RESEARCH_REPLAY_NOT_REALIZED_RETURNS"}
