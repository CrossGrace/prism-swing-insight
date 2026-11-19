"""Forward-only PRISM MCP paper study: no backfill, orders or live broker writes.

This is an isolated contrarian research lane. PRISM owns the read-only KIS MCP
price source and its MarketPulse classification. A frozen deterministic rule
owns the paper decision; no historical winner is imported into its ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from swing_insight.market import MarketDataUnavailable, evaluate_market, normalized_bars
from swing_insight.simulator import Candle, ReplayPolicy, _candles, _exit_fill, _rsi
from swing_insight.sources import get_market_snapshots

KST = ZoneInfo("Asia/Seoul")
_CLOSE_READY = time(16, 10)
_START_TICKER = "010060"
_GENESIS = "0" * 64


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _code_hash() -> str:
    return _sha(Path(__file__).read_bytes())


def _now() -> datetime:
    return datetime.now(KST)


@dataclass(frozen=True)
class BlindPolicy:
    start_date: str
    initial_cash: float
    stop_pct: float
    take_pct: float
    max_hold_days: int
    fee_bps: float
    sell_tax_bps: float
    slippage_bps: float
    ticker: str = _START_TICKER
    daily_drop_min_pct: float = 6.0
    rsi_max: float = 40.0
    strategy: str = "RED6_RSI14_WILDER_NEXT_OPEN_V1"
    data_source: str = "PRISM_KIS_MCP_DAILY_OHLCV"
    market_pulse_role: str = "OBSERVE_ONLY_NOT_A_BUY_VETO"

    def __post_init__(self) -> None:
        values = (self.initial_cash, self.stop_pct, self.take_pct, self.fee_bps,
                  self.sell_tax_bps, self.slippage_bps, self.daily_drop_min_pct, self.rsi_max)
        if (self.ticker != _START_TICKER or not all(math.isfinite(x) for x in values)
                or self.initial_cash <= 0 or not 0 < self.stop_pct < 100
                or not 0 < self.take_pct < 100 or self.max_hold_days not in (0, 1, 2, 3)
                or min(self.fee_bps, self.sell_tax_bps, self.slippage_bps) < 0
                or self.slippage_bps >= 10000
                or self.daily_drop_min_pct != 6 or self.rsi_max != 40
                or self.strategy != "RED6_RSI14_WILDER_NEXT_OPEN_V1"
                or self.data_source != "PRISM_KIS_MCP_DAILY_OHLCV"
                or self.market_pulse_role != "OBSERVE_ONLY_NOT_A_BUY_VETO"):
            raise ValueError("Invalid or mutable blind experiment contract")
        if date.fromisoformat(self.start_date).weekday() >= 5:
            raise ValueError("BLIND_START_MUST_BE_A_WEEKDAY")

    @property
    def fingerprint(self) -> str:
        return _sha(_json(asdict(self)).encode())


def _event_hash(previous: str, payload: dict[str, Any]) -> str:
    return _sha((previous + ":" + _json(payload)).encode())


def _estimated_liquidation(state: dict[str, Any], policy: BlindPolicy) -> float:
    """Mark open paper inventory at last daily close, net of assumed sell costs."""
    value = float(state["cash"])
    position = state["position"]
    if position is not None:
        value += (position["shares"] * state["last_close"]
                  * (1 - policy.slippage_bps / 10000)
                  * (1 - (policy.fee_bps + policy.sell_tax_bps) / 10000))
    return value


class BlindStudy:
    """Hash-chained paper events; each step must be a new real-world KST session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self, policy: BlindPolicy, *, now: datetime | None = None) -> dict[str, Any]:
        now = (now or _now()).astimezone(KST)
        if date.fromisoformat(policy.start_date) <= now.date():
            raise ValueError("BLIND_START_MUST_BE_IN_THE_FUTURE")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS blind_study (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    policy TEXT NOT NULL,
                    policy_sha TEXT NOT NULL,
                    code_sha TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blind_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS blind_events_no_update
                    BEFORE UPDATE ON blind_events BEGIN
                    SELECT RAISE(ABORT, 'BLIND_EVENTS_IMMUTABLE');
                    END;
                CREATE TRIGGER IF NOT EXISTS blind_events_no_delete
                    BEFORE DELETE ON blind_events BEGIN
                    SELECT RAISE(ABORT, 'BLIND_EVENTS_IMMUTABLE');
                    END;
            """)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM blind_study WHERE id=1").fetchone():
                raise ValueError("BLIND_STUDY_ALREADY_INITIALIZED")
            conn.execute("INSERT INTO blind_study VALUES (1, ?, ?, ?, ?)",
                         (_json(asdict(policy)), policy.fingerprint, _code_hash(), now.isoformat()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"status": "PREPARED_NOT_RUNNING", "ticker": policy.ticker,
                "first_signal_date": policy.start_date, "policy_sha256": policy.fingerprint,
                "live_orders": False, "execution": "FORWARD_ONLY_DAILY_PAPER"}

    @staticmethod
    def _fold(rows: list[sqlite3.Row], policy: BlindPolicy) -> tuple[dict[str, Any], str]:
        state: dict[str, Any] = {"cash": policy.initial_cash, "position": None,
                                 "pending": None, "last_date": None, "last_close": None}
        previous = _GENESIS
        for row in rows:
            payload = json.loads(row["payload"])
            if row["previous_hash"] != previous or row["event_hash"] != _event_hash(previous, payload):
                raise ValueError("BLIND_AUDIT_HASH_CHAIN_BROKEN")
            previous = row["event_hash"]
            if state["last_date"] is not None and payload["as_of"] <= state["last_date"]:
                raise ValueError("BLIND_AUDIT_NONMONOTONIC")
            state = payload["state_after"]
        return state, previous

    def _read(self, conn: sqlite3.Connection) -> tuple[BlindPolicy, dict[str, Any], str]:
        row = conn.execute("SELECT * FROM blind_study WHERE id=1").fetchone()
        if row is None:
            raise ValueError("BLIND_STUDY_NOT_INITIALIZED")
        policy = BlindPolicy(**json.loads(row["policy"]))
        if policy.fingerprint != row["policy_sha"] or row["code_sha"] != _code_hash():
            raise ValueError("BLIND_POLICY_OR_CODE_CHANGED_REQUIRES_NEW_STUDY")
        rows = conn.execute("SELECT * FROM blind_events ORDER BY id").fetchall()
        state, previous = self._fold(rows, policy)
        return policy, state, previous

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            raise ValueError("BLIND_STUDY_NOT_INITIALIZED")
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            policy, state, head = self._read(conn)
            entries = conn.execute("SELECT COUNT(*) FROM blind_events").fetchone()[0]
            rows = conn.execute("SELECT payload FROM blind_events ORDER BY id").fetchall()
        events = [json.loads(row["payload"]) for row in rows]
        returns = [act["net_pct"] for event in events
                   for act in event["actions"]
                   if act["type"] == "PAPER_SELL_DAILY_ESTIMATE"]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        liquidation_value = _estimated_liquidation(state, policy)
        peak, max_drawdown = policy.initial_cash, 0.0
        for event in events:
            equity = _estimated_liquidation(event["state_after"], policy)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, 100 * (1 - equity / peak))
        return {"status": "PREPARED_NOT_RUNNING" if not entries else "PAPER_OBSERVATION_STARTED",
                "strategy": policy.strategy, "ticker": policy.ticker, "start_date": policy.start_date,
                "last_observed_date": state["last_date"], "cash": round(state["cash"], 2),
                "position": state["position"], "pending": state["pending"],
                "estimated_liquidation_equity": round(liquidation_value, 2),
                "estimated_net_return_pct": (round(100 * (liquidation_value / policy.initial_cash - 1), 3)
                                             if entries else None),
                "estimated_max_drawdown_pct": round(max_drawdown, 3) if entries else None,
                "completed_paper_trades": len(returns), "wins": len(wins), "losses": len(losses),
                "mean_win_pct": round(sum(wins) / len(wins), 3) if wins else None,
                "mean_loss_pct": round(sum(losses) / len(losses), 3) if losses else None,
                "win_loss_payoff_ratio": (round(sum(wins) / len(wins) / abs(sum(losses) / len(losses)), 3)
                                          if wins and losses and sum(losses) != 0 else None),
                "policy_sha256": policy.fingerprint, "audit_head_sha256": head,
                "recorded_sessions": entries, "live_orders": False}

    async def step(self, as_of: str, *, now: datetime | None = None,
                   fetcher=get_market_snapshots) -> dict[str, Any]:
        """One session, after its close. Never backfill a past observation."""
        moment = (now or _now()).astimezone(KST)
        target = date.fromisoformat(as_of)
        if target != moment.date() or moment.time() < _CLOSE_READY:
            raise ValueError("BLIND_STEP_ONLY_TODAY_AFTER_1610_KST")
        if not self.path.exists():
            raise ValueError("BLIND_STUDY_NOT_INITIALIZED")
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            policy, prev_state, _ = self._read(conn)
        if target < date.fromisoformat(policy.start_date):
            raise ValueError("BLIND_STEP_BEFORE_START_DATE")
        if prev_state["last_date"] is not None and as_of <= prev_state["last_date"]:
            raise ValueError("BLIND_STEP_ALREADY_PROCESSED_OR_NONMONOTONIC")

        # PRISM is the authoritative read-only source. No public proxy or
        # generated price path is allowed in the forward study.
        try:
            stock, index = await fetcher(policy.ticker, as_of)
        except Exception as exc:  # noqa: BLE001 - do not echo MCP config/credential errors
            raise ValueError("BLIND_PRISM_MCP_DAILY_SOURCE_UNAVAILABLE") from exc
        if any(k > as_of for data in (stock, index) for k in data if k != "__meta__"
               and isinstance(k, str) and len(k) == 10 and k[4] == "-"):
            raise ValueError("BLIND_FUTURE_BAR_FROM_SOURCE")
        stock_bars = _candles(stock, as_of)
        obs = evaluate_market(ticker=policy.ticker, as_of=as_of, stock=stock, index=index)
        if (obs.source_status != "VERIFIED_DAILY" or obs.price_date != as_of
                or obs.index_date != as_of):
            raise MarketDataUnavailable("BLIND_PRISM_DAILY_DATA_NOT_CONFIRMED")
        sessions = [b for b in stock_bars if (prev_state["last_date"] or policy.start_date)
                    <= b.date <= as_of]
        index_sessions = [b.date for b in normalized_bars(index, as_of=as_of)
                          if ((policy.start_date <= b.date) if prev_state["last_date"] is None
                              else (prev_state["last_date"] < b.date)) and b.date <= as_of]
        # First step needs exactly today's bar; later steps need the prior
        # recorded bar and exactly one new session. A missed market day cannot
        # be reconstructed and relabelled as a real forward observation.
        if prev_state["last_date"] is None:
            sessions = [b for b in stock_bars if policy.start_date <= b.date <= as_of]
            if len(sessions) != 1 or index_sessions != [as_of]:
                raise ValueError("BLIND_MISSED_SESSION_CANNOT_BACKFILL")
        elif (len(sessions) != 2 or sessions[-2].date != prev_state["last_date"]
              or index_sessions != [as_of]):
            raise ValueError("BLIND_MISSED_SESSION_CANNOT_BACKFILL")

        state = json.loads(_json(prev_state))
        candle = stock_bars[-1]
        actions: list[dict[str, Any]] = []
        pending = state["pending"]
        if pending:
            buy_price = candle.open * (1 + policy.slippage_bps / 10000)
            fee_ratio = policy.fee_bps / 10000
            shares = math.floor(state["cash"] / (buy_price * (1 + fee_ratio)))
            if shares < 1:
                actions.append({"type": "SKIP_INSUFFICIENT_CASH", "signal_date": pending["signal_date"]})
            else:
                spent = shares * buy_price * (1 + fee_ratio)
                state["cash"] -= spent
                state["position"] = {"shares": shares, "entry_date": as_of,
                                     "signal_date": pending["signal_date"],
                                     "buy_price": buy_price, "entry_cost": spent}
                actions.append({"type": "PAPER_BUY_NEXT_OPEN", "shares": shares,
                                "price": round(buy_price, 4),
                                "signal_date": pending["signal_date"]})
            state["pending"] = None

        position = state["position"]
        if position:
            entry_idx = next((i for i, b in enumerate(stock_bars)
                              if b.date == position["entry_date"]), None)
            if entry_idx is None:
                raise ValueError("BLIND_ENTRY_BAR_MISSING_FROM_SOURCE")
            hold = len(stock_bars) - entry_idx - 1
            if hold > policy.max_hold_days:
                raise ValueError("BLIND_POSITION_EXIT_SESSION_MISSED")
            simulation = ReplayPolicy(policy.rsi_max, 2, policy.stop_pct,
                                      policy.take_pct, policy.max_hold_days,
                                      policy.fee_bps, policy.sell_tax_bps,
                                      policy.slippage_bps)
            fill = _exit_fill(candle, position["buy_price"], simulation, hold)
            if fill:
                reference, reason = fill
                sale = reference * (1 - policy.slippage_bps / 10000)
                shares = position["shares"]
                proceeds = shares * sale * (1 - (policy.fee_bps + policy.sell_tax_bps) / 10000)
                pnl_pct = (proceeds / position["entry_cost"] - 1) * 100
                state["cash"] += proceeds
                actions.append({"type": "PAPER_SELL_DAILY_ESTIMATE", "date": as_of,
                                "shares": shares, "price": round(sale, 4),
                                "reason": reason, "net_pct": round(pnl_pct, 4),
                                "uncertainty": "NO_MINUTE_SEQUENCE;STOP_FIRST_IF_COLLISION"})
                state["position"] = None

        closes = [b.close for b in stock_bars]
        rsi = _rsi(closes)
        daily_change_pct = (closes[-1] / closes[-2] - 1) * 100
        if state["position"] is None and state["pending"] is None and (
                rsi is not None and rsi <= policy.rsi_max
                and daily_change_pct <= -policy.daily_drop_min_pct):
            state["pending"] = {"signal_date": as_of, "rsi14": round(rsi, 3),
                                "daily_change_pct": round(daily_change_pct, 3)}
            actions.append({"type": "PAPER_SIGNAL_NEXT_SESSION_OPEN",
                            "rsi14": round(rsi, 3),
                            "daily_change_pct": round(daily_change_pct, 3)})

        state["last_date"] = as_of
        state["last_close"] = candle.close
        snapshot = {"as_of": as_of, "recorded_at": moment.isoformat(),
                    "data_source": policy.data_source,
                    "market": {"prism_market_pulse": obs.index_state,
                               "distribution_days": obs.distribution_days,
                               "role": policy.market_pulse_role},
                    "price": {"open": candle.open, "high": candle.high,
                              "low": candle.low, "close": candle.close,
                              "volume": candle.volume},
                    "indicator": {"rsi14": round(rsi, 4) if rsi is not None else None,
                                  "change_1d_pct": round(daily_change_pct, 4)},
                    "source_sha256": _sha(_json({"stock": stock, "index": index}).encode()),
                    "source_snapshot": {"stock": stock, "index": index},
                    "actions": actions, "state_after": state}
        with sqlite3.connect(self.path, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            current_policy, current_state, previous = self._read(conn)
            if current_policy.fingerprint != policy.fingerprint or current_state != prev_state:
                raise ValueError("BLIND_CONCURRENT_STEP_CONFLICT")
            hashed = _event_hash(previous, snapshot)
            conn.execute("""INSERT INTO blind_events(date, payload, previous_hash, event_hash)
                            VALUES (?, ?, ?, ?)""",
                         (as_of, _json(snapshot), previous, hashed))
        return {"status": "FORWARD_PAPER_SESSION_RECORDED", "as_of": as_of,
                "prism_market_state": obs.index_state, "rsi14": snapshot["indicator"]["rsi14"],
                "actions": actions, "cash": round(state["cash"], 2),
                "pending": state["pending"], "position": state["position"],
                "audit_head_sha256": hashed, "live_orders": False,
                "fill_quality": "DAILY_OHLC_CONSERVATIVE_NOT_BROKER_EXECUTION"}
