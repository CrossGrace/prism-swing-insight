"""Independent audit trail and *paper-only* account; never imports broker clients."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swing_insight.analyst import ThesisOpinion
from swing_insight.market import MarketObservation
from swing_insight.watchlist import Evidence, WatchItem


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PaperPolicy:
    initial_cash: float
    single_position_limit: float
    total_exposure_limit: float
    fee_bps: float
    sell_tax_bps: float

    def __post_init__(self) -> None:
        values = (self.initial_cash, self.single_position_limit, self.total_exposure_limit,
                  self.fee_bps, self.sell_tax_bps)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Paper policy needs finite values")
        if self.initial_cash <= 0 or not (0 < self.single_position_limit <= self.total_exposure_limit <= 1):
            raise ValueError("Explicit cash and exposure limits are required")
        if self.fee_bps < 0 or self.sell_tax_bps < 0:
            raise ValueError("Trading costs cannot be negative")


class SwingJournal:
    """Local SQLite store; paper fills are synthetic close-price fills only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS thesis_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    thesis_hash TEXT NOT NULL,
                    opinion_status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS swing_review_idx ON thesis_reviews(ticker, id);
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_cash REAL NOT NULL,
                    single_limit REAL NOT NULL,
                    total_limit REAL NOT NULL,
                    fee_bps REAL NOT NULL,
                    sell_tax_bps REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    shares INTEGER NOT NULL CHECK (shares > 0),
                    price REAL NOT NULL CHECK (price > 0),
                    fee REAL NOT NULL,
                    tax REAL NOT NULL,
                    reference_date TEXT NOT NULL,
                    review_id INTEGER NOT NULL REFERENCES thesis_reviews(id),
                    recorded_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _thesis_hash(item: WatchItem) -> str:
        text = json.dumps({"thesis": item.thesis, "invalidation": item.invalidation},
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def append_review(self, item: WatchItem, market: MarketObservation, opinion: ThesisOpinion,
                      *, news_titles: str = "", additional_evidence: tuple[Evidence, ...] = ()) -> dict[str, Any]:
        """Append-only, versioned review. Never overwrite an earlier opinion."""
        if item.ticker != market.ticker:
            raise ValueError("Mismatched review ticker")
        thesis_hash = self._thesis_hash(item)
        record = {"ticker": item.ticker, "company": item.company, "as_of": market.as_of,
                  "thesis": item.thesis, "invalidation": item.invalidation,
                  "evidence": [vars(e) for e in (*item.evidence, *additional_evidence)
                               if e.as_of <= market.as_of],
                  "market": market.as_dict(), "opinion": opinion.as_dict(),
                  "news_context": news_titles[:4000], "recorded_at": _utc_now()}
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT thesis_hash, opinion_status FROM thesis_reviews WHERE ticker=? ORDER BY id DESC LIMIT 1",
                (item.ticker,),
            ).fetchone()
            cursor = conn.execute("""
                INSERT INTO thesis_reviews(ticker, as_of, recorded_at, thesis_hash, opinion_status, payload)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (item.ticker, market.as_of, record["recorded_at"], thesis_hash,
                  opinion.thesis_status, json.dumps(record, ensure_ascii=False)))
            record["review_id"] = cursor.lastrowid
            record["thesis_changed"] = bool(previous and previous["thesis_hash"] != thesis_hash)
            record["opinion_changed"] = bool(previous and previous["opinion_status"] != opinion.thesis_status)
        return record

    def latest_review(self, ticker: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id, payload FROM thesis_reviews WHERE ticker=? ORDER BY id DESC LIMIT 1",
                               (ticker,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["payload"]), "review_id": row["id"]}

    def initialize_paper(self, policy: PaperPolicy) -> None:
        """Creates an empty paper account once; requires explicit user risk budget."""
        with self._connect() as conn:
            conn.execute("""INSERT INTO paper_account
                (id, initial_cash, single_limit, total_limit, fee_bps, sell_tax_bps)
                VALUES (1, ?, ?, ?, ?, ?)""",
                (policy.initial_cash, policy.single_position_limit, policy.total_exposure_limit,
                 policy.fee_bps, policy.sell_tax_bps))

    @staticmethod
    def _positions(rows: list[sqlite3.Row]) -> tuple[dict[str, int], float, dict[str, float]]:
        holdings: dict[str, int] = {}
        cash_change = 0.0
        basis: dict[str, float] = {}
        for trade in rows:
            ticker, shares, price = trade["ticker"], trade["shares"], trade["price"]
            fee, tax = trade["fee"], trade["tax"]
            owned = holdings.get(ticker, 0)
            if trade["side"] == "BUY":
                holdings[ticker] = owned + shares
                basis[ticker] = basis.get(ticker, 0.0) + shares * price + fee
                cash_change -= shares * price + fee
            else:
                if owned < shares:
                    raise ValueError("Corrupt paper ledger: short position")
                avg = basis[ticker] / owned
                basis[ticker] -= avg * shares
                holdings[ticker] = owned - shares
                cash_change += shares * price - fee - tax
                if holdings[ticker] == 0:
                    del holdings[ticker]
                    del basis[ticker]
        return holdings, cash_change, basis

    def paper_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            account = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            if account is None:
                raise ValueError("Initialize the paper account and risk budget first")
            rows = conn.execute("SELECT * FROM paper_fills ORDER BY id").fetchall()
            holdings, delta_cash, basis = self._positions(rows)
            cash = account["initial_cash"] + delta_cash
            # No old/future quote is silently used: only the most recent dated review.
            quotes: dict[str, dict[str, Any]] = {}
            for ticker in holdings:
                row = conn.execute("""SELECT payload FROM thesis_reviews WHERE ticker=?
                                      ORDER BY as_of DESC, id DESC LIMIT 1""", (ticker,)).fetchone()
                if row:
                    m = json.loads(row["payload"])["market"]
                    quotes[ticker] = {"date": m["price_date"], "close": m["stock_close"]}
            complete = all(ticker in quotes and quotes[ticker]["close"] is not None
                           for ticker in holdings)
            market_value = (sum(qty * quotes[ticker]["close"] for ticker, qty in holdings.items())
                            if complete else None)
            equity = cash + market_value if market_value is not None else None
            return {"initial_cash": account["initial_cash"], "cash": round(cash, 2),
                    "holdings": holdings, "cost_basis": basis, "quotes": quotes,
                    "market_value": round(market_value, 2) if market_value is not None else None,
                    "equity": round(equity, 2) if equity is not None else None,
                    "return_pct": round((equity / account["initial_cash"] - 1) * 100, 3)
                                  if equity is not None else None,
                    "fills": len(rows), "valuation": "EOD_REFERENCE_ONLY_NOT_EXECUTABLE"}

    def paper_fill(self, *, ticker: str, side: str, shares: int, as_of: str,
                   request_id: str, current_tickers: set[str]) -> dict[str, Any]:
        """Manual synthetic close fill, not a trading signal or a broker operation.

        Unique request_id makes retries idempotent. Sells remain available in
        correction markets. No fee, tax or position weights are inferred.
        """
        if side not in {"BUY", "SELL"} or type(shares) is not int or shares <= 0:
            raise ValueError("Use BUY/SELL and a positive integer share count")
        if not request_id or len(request_id) > 128:
            raise ValueError("Supply a stable idempotency key")
        if ticker not in current_tickers or not 1 <= len(current_tickers) <= 2:
            raise ValueError("Ticker is not in the current manually approved 1-2 stock watchlist")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM paper_fills WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (existing["ticker"], existing["side"], existing["shares"], existing["reference_date"]) != (
                    ticker, side, shares, as_of
                ):
                    raise ValueError("Idempotency key reused for a different paper order")
                return {"status": "ALREADY_RECORDED", "fill_id": existing["id"]}
            acc = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            if not acc:
                raise ValueError("Paper account is not initialized")
            review = conn.execute("""SELECT id, payload, thesis_hash FROM thesis_reviews
                                  WHERE ticker=? AND as_of=? ORDER BY id DESC LIMIT 1""",
                                  (ticker, as_of)).fetchone()
            if not review:
                raise ValueError("No as-of research review for paper fill")
            payload = json.loads(review["payload"])
            market, opinion = payload["market"], payload["opinion"]
            price = market.get("stock_close")
            if (not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0
                    or market.get("price_date") != as_of):
                raise ValueError("Missing verified end-of-day close for paper fill")
            all_rows = conn.execute("SELECT * FROM paper_fills ORDER BY id").fetchall()
            holdings, delta_cash, _ = self._positions(all_rows)
            cash = acc["initial_cash"] + delta_cash
            cost = shares * price
            fee = round(cost * acc["fee_bps"] / 10000, 2)
            tax = round(cost * acc["sell_tax_bps"] / 10000, 2) if side == "SELL" else 0.0

            if side == "BUY":
                if not market.get("new_entries_allowed"):
                    raise ValueError("BUY_BLOCKED_MARKET_OR_DATA")
                if opinion["thesis_status"] != "INTACT" or not payload.get("evidence"):
                    raise ValueError("BUY_BLOCKED_FUNDAMENTAL_REVIEW")
                if not holdings.get(ticker) and len(holdings) >= 2:
                    raise ValueError("BUY_BLOCKED_MAX_TWO_POSITIONS")
                if cash + 1e-6 < cost + fee:
                    raise ValueError("BUY_BLOCKED_INSUFFICIENT_CASH")
                # Current holdings use last known close. Missing or later-dated
                # quotes are not valid for risk sizing at this decision date.
                current_value = 0.0
                ticker_value = holdings.get(ticker, 0) * price
                for other, qty in holdings.items():
                    if other == ticker:
                        current_value += ticker_value
                        continue
                    r = conn.execute("""SELECT payload FROM thesis_reviews WHERE ticker=?
                                     AND as_of<=? ORDER BY as_of DESC,id DESC LIMIT 1""",
                                     (other, as_of)).fetchone()
                    if not r:
                        raise ValueError("BUY_BLOCKED_MISSING_PORTFOLIO_QUOTE")
                    other_market = json.loads(r["payload"])["market"]
                    if other_market.get("price_date") != as_of or not other_market.get("stock_close"):
                        raise ValueError("BUY_BLOCKED_STALE_PORTFOLIO_QUOTE")
                    current_value += qty * other_market["stock_close"]
                equity = cash + current_value
                if ((ticker_value + cost) / equity > acc["single_limit"] + 1e-9
                        or (current_value + cost) / equity > acc["total_limit"] + 1e-9):
                    raise ValueError("BUY_BLOCKED_EXPOSURE_LIMIT")
            elif holdings.get(ticker, 0) < shares:
                raise ValueError("SELL_BLOCKED_INSUFFICIENT_HOLDINGS")

            fill = conn.execute("""INSERT INTO paper_fills
                (request_id, ticker, side, shares, price, fee, tax, reference_date, review_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, ticker, side, shares, price, fee, tax, as_of,
                 review["id"], _utc_now()))
            return {"status": "PAPER_FILLED_AT_REFERENCE_CLOSE", "fill_id": fill.lastrowid,
                    "ticker": ticker, "side": side, "shares": shares, "reference_close": price,
                    "fee": fee, "tax": tax, "as_of": as_of}
