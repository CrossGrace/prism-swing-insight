"""Historical paper ledger for the PRISM Swing sharp-drop recovery policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

from cores.market_pulse import DailyBar, MarketPulse
from swing_insight.market import normalized_bars
from swing_insight.simulator import (
    Candle, PendingEntry, RecoveryPolicy, ReplayPolicy, _candles, _entry_today,
    _signal_rsi, _tactical_fraction,
)

BASELINE = ReplayPolicy(45, 2, 3, 5, 3, 10, 20, 10)
SHARP = RecoveryPolicy(gap_entry=True, min_daily_drop_pct=6)
INITIAL_CASH = 10_000_000
_STEPS = (2, 3, 5, 7)


@dataclass(frozen=True)
class ScalePolicy:
    entry_parts: int = 1
    entry_gap_pct: float = 0.4
    entry_weights: str = "equal"
    entry_mode: str = "limit"
    exit_parts: int = 2
    exit_gap_pct: float = 0.75
    exit_weights: str = "equal"
    trail_pct: float = 5.0

    def __post_init__(self) -> None:
        if (type(self.entry_parts) is not int or self.entry_parts not in (1, *_STEPS)
                or type(self.exit_parts) is not int or self.exit_parts not in _STEPS
                or self.entry_mode not in {"limit", "confirmed"}
                or not all(weight in {"equal", "front"} for weight in
                           (self.entry_weights, self.exit_weights))
                or not all(math.isfinite(x) and 0 < x < 100 for x in
                           (self.entry_gap_pct, self.exit_gap_pct, self.trail_pct))):
            raise ValueError("INVALID_SCALE_POLICY")


def _weights(parts: int, style: str) -> tuple[float, ...]:
    if style == "equal" or parts == 1:
        return (1 / parts,) * parts
    return (0.5,) + (0.5 / (parts - 1),) * (parts - 1)


@dataclass
class Position:
    signal: PendingEntry
    day: str
    session: int
    anchor: float
    stop: float
    budget: float
    shares: int = 0
    bought: int = 0
    next_buy: int = 0
    next_target: int = 0
    buy_closed: bool = False
    confirmed: bool = False
    spent: float = 0.0
    received: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)


class Account:
    def __init__(self, policy: ReplayPolicy, scale: ScalePolicy, trade_through_pct: float):
        self.policy = policy
        self.scale = scale
        self.cash = float(INITIAL_CASH)
        self.fee = policy.fee_bps / 10_000
        self.tax = policy.sell_tax_bps / 10_000
        self.slip = policy.slippage_bps / 10_000
        self.through = trade_through_pct / 100
        self.entry_weights = _weights(scale.entry_parts, scale.entry_weights)
        self.exit_weights = _weights(scale.exit_parts, scale.exit_weights)
        self.zero_buy = self.zero_sale = 0

    def buy(self, position: Position, day: str, price: float, reason: str) -> bool:
        allocation = position.budget * self.entry_weights[position.next_buy]
        if position.next_buy == len(self.entry_weights) - 1:
            allocation = position.budget - position.spent
        fill = price * (1 + self.slip)
        unit = fill * (1 + self.fee)
        shares = math.floor(min(allocation, self.cash, position.budget - position.spent) / unit)
        position.next_buy += 1
        if shares <= 0:
            self.zero_buy += 1
            return False
        gross = shares * fill
        fee = shares * (unit - fill)
        debit = shares * unit
        self.cash -= debit
        position.spent += debit
        position.bought += shares
        position.shares += shares
        self.log(position, day, "BUY", reason, shares, price, fill, fee, 0.0, gross)
        return True

    def sell(self, position: Position, day: str, price: float, shares: int, reason: str) -> None:
        if not 0 < shares <= position.shares:
            raise ValueError("INVALID_INVENTORY_DEBIT")
        fill = price * (1 - self.slip)
        gross = shares * fill
        fee, tax = gross * self.fee, gross * self.tax
        net = gross - fee - tax
        self.cash += net
        position.received += net
        position.shares -= shares
        self.log(position, day, "SELL", reason, shares, price, fill, fee, tax, gross)

    def log(self, position: Position, day: str, side: str, reason: str, shares: int,
            reference: float, fill: float, fee: float, tax: float, gross: float) -> None:
        position.fills.append({
            "date": day, "side": side, "reason": reason, "shares": shares,
            "price": fill, "gross": gross, "fee": fee, "tax": tax,
            "slippage": abs(reference - fill) * shares,
            "cash_after": self.cash, "shares_after": position.shares,
        })

    def add_level(self, position: Position) -> float:
        direction = -1 if self.scale.entry_mode == "limit" else 1
        return position.anchor * (1 + direction * position.next_buy * self.scale.entry_gap_pct / 100)

    def limits(self, position: Position, day: str, observed: float, *, opening: bool) -> bool:
        added = False
        while not position.buy_closed and position.next_buy < self.scale.entry_parts:
            level = self.add_level(position)
            if level <= position.stop or observed > level * (1 - self.through):
                break
            reference = min(level, observed) if opening else level
            added |= self.buy(position, day, reference, "ADD_LIMIT")
        return added

    def targets(self, position: Position, day: str, observed: float) -> None:
        while position.next_target < self.scale.exit_parts - 1:
            step = position.next_target
            target = position.anchor * (1 + (self.policy.take_profit_pct
                                              + step * self.scale.exit_gap_pct) / 100)
            if observed < target * (1 + self.through):
                break
            position.next_target += 1
            cumulative = math.floor(position.bought * math.fsum(self.exit_weights[:step + 1]))
            shares = cumulative - (position.bought - position.shares)
            if shares <= 0:
                self.zero_sale += 1
                continue
            self.sell(position, day, target, shares, "PARTIAL_TAKE")
            position.buy_closed = True

    def process(self, position: Position, bar: Candle, hold: int) -> None:
        if hold and bar.open <= position.stop:
            self.sell(position, bar.date, bar.open, position.shares, "STOP_GAP")
            return
        can_add = hold and not position.buy_closed and position.next_buy < self.scale.entry_parts
        confirmed, position.confirmed = position.confirmed, False
        if hold:
            self.targets(position, bar.date, bar.open)
        if can_add and not position.buy_closed:
            if self.scale.entry_mode == "limit":
                self.limits(position, bar.date, bar.open, opening=True)
            elif confirmed and bar.open <= self.add_level(position) * (1 + self.scale.entry_gap_pct / 100):
                self.buy(position, bar.date, bar.open, "ADD_CONFIRMED_OPEN")
        intraday_add = bool(can_add and self.scale.entry_mode == "limit"
                            and self.limits(position, bar.date, bar.low, opening=False))
        if bar.low <= position.stop:
            self.sell(position, bar.date, position.stop, position.shares, "STOP_FIRST")
            return
        if not intraday_add:
            self.targets(position, bar.date, bar.high)
        if hold == self.policy.max_hold_days:
            self.sell(position, bar.date, bar.close, position.shares, "TIME_EXIT")
        elif position.buy_closed:
            position.stop = max(position.stop, bar.close * (1 - self.scale.trail_pct / 100))
        elif (self.scale.entry_mode == "confirmed" and position.next_buy < self.scale.entry_parts
              and bar.close >= self.add_level(position)):
            position.confirmed = True

    def value(self, position: Position | None, mark: float) -> float:
        if position is None:
            return self.cash
        return self.cash + position.shares * mark * (1 - self.slip) * (1 - self.fee - self.tax)


def replay_account(*, stock: dict, index: dict, start: str, end: str,
                   scale: ScalePolicy = ScalePolicy(), policy: ReplayPolicy = BASELINE,
                   trade_through_pct: float = 0.0) -> dict[str, Any]:
    """Feed one session at a time. A candidate cannot access any future price."""
    if not 0 <= trade_through_pct < 100 or policy.fee_bps + policy.sell_tax_bps >= 10_000:
        raise ValueError("INVALID_EXECUTION_COST")
    if any(day > end for data in (stock, index) for day in data if day != "__meta__"):
        raise ValueError("SCALE_FUTURE_BAR_FROM_SOURCE")
    candles = _candles(stock, end)
    market = normalized_bars(index, as_of=end)
    sessions = [bar.date for bar in market]
    if [bar.date for bar in candles] != sessions:
        raise ValueError("SCALE_STOCK_INDEX_SESSION_MISMATCH")
    if start not in sessions or end not in sessions or start > end:
        raise ValueError("SCALE_WINDOW_MUST_BE_COVERED_BY_SESSIONS")
    first, last = sessions.index(start), len(sessions) - 1
    if first < 30:
        raise ValueError("SCALE_INSUFFICIENT_START_WARMUP")
    last_signal = last - SHARP.wait_sessions - policy.max_hold_days
    account = Account(policy, scale, trade_through_pct)
    cash_curve = []
    trades = []
    blocked = []
    pending: list[PendingEntry] = []
    closes: list[float] = []
    market_closes: list[float] = []
    pulse = MarketPulse()
    position = None
    previous = candles[0]
    for session, (bar, index_bar) in enumerate(zip(candles, market)):
        pulse.feed(DailyBar(index_bar.date, index_bar.close, index_bar.volume))
        market_closes.append(index_bar.close)
        closes.append(bar.close)
        if session < first:
            previous = bar
            continue

        pending = [signal for signal in pending if session - signal.signal_idx <= SHARP.wait_sessions]
        if position is None:
            entry = _entry_today(pending, session, bar, previous, policy, SHARP)
            if entry is not None:
                signal, reference, basis = entry
                anchor = reference * (1 + account.slip)
                proposed = Position(signal, bar.date, session, anchor,
                                    anchor * (1 - policy.stop_loss_pct / 100),
                                    account.cash * signal.position_fraction)
                if account.buy(proposed, bar.date, reference, basis):
                    position = proposed
                pending.clear()
        previous = bar
        if position is not None:
            hold = session - position.session
            account.process(position, bar, hold)
            if not position.shares:
                trades.append({
                    "signal_date": position.signal.signal_date, "entry_date": position.day,
                    "exit_date": bar.date, "market_state": position.signal.market_state,
                    "sleeve_fraction": position.signal.position_fraction, "hold_days": hold,
                    "allocation": position.budget, "spent": position.spent,
                    "net_krw": position.received - position.spent,
                    "net_pct": 100 * (position.received / position.spent - 1),
                    "fills": position.fills,
                })
                position = None
        elif session <= last_signal:
            rsi = _signal_rsi(closes, policy, SHARP)
            if rsi is not None:
                fraction, reason = _tactical_fraction(pulse.state, market_closes)
                if fraction:
                    pending.append(PendingEntry(session, bar.date, pulse.state, fraction, rsi))
                else:
                    blocked.append({"date": bar.date, "reason": reason})
        cash_curve.append({"date": bar.date, "equity": account.value(position, bar.close)})

    if position is not None or pending:
        raise AssertionError("SCALE_UNSETTLED_WINDOW_BOUNDARY")
    peak = INITIAL_CASH
    drawdown = 0.0
    for point in cash_curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, 100 * (1 - point["equity"] / peak))
    wins = [trade["net_pct"] for trade in trades if trade["net_krw"] > 0]
    losses = [trade["net_pct"] for trade in trades if trade["net_krw"] <= 0]
    fills = [fill for trade in trades for fill in trade["fills"]]
    buys = [sum(fill["side"] == "BUY" for fill in trade["fills"]) for trade in trades]
    sells = [sum(fill["side"] == "SELL" for fill in trade["fills"]) for trade in trades]
    return {
        "start": start, "end": end, "sessions": len(cash_curve), "scale": asdict(scale),
        "policy": asdict(policy), "trade_through_pct": trade_through_pct,
        "initial_cash": INITIAL_CASH, "ending_cash": account.cash,
        "net_pct": 100 * (account.cash / INITIAL_CASH - 1), "mdd_pct": drawdown,
        "trade_count": len(trades), "wins": len(wins), "losses": len(losses),
        "win_pct": 100 * len(wins) / len(trades) if trades else None,
        "mean_win_pct": sum(wins) / len(wins) if wins else None,
        "mean_loss_pct": sum(losses) / len(losses) if losses else None,
        "fill_count": len(fills), "buy_count": sum(buys), "sell_count": sum(sells),
        "fully_scaled_entries": buys.count(scale.entry_parts),
        "fully_scaled_exits": sells.count(scale.exit_parts),
        "zero_share_buy_attempts": account.zero_buy, "zero_share_targets": account.zero_sale,
        "fees": sum(fill["fee"] for fill in fills), "tax": sum(fill["tax"] for fill in fills),
        "slippage": sum(fill["slippage"] for fill in fills),
        "trades": trades, "blocked": blocked, "equity": cash_curve,
    }
