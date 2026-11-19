"""Strictly offline forward paper-contract tests. No KIS login or order agents."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from swing_insight.blind import KST, BlindPolicy, BlindStudy
from swing_insight.market import MarketDataUnavailable


def policy():
    return BlindPolicy("2026-11-20", 1_000_000, 5, 8, 3, 10, 20, 10)


def clock(day: int, hour: int = 16):
    return datetime(2026, 11, day, hour, 30, tzinfo=KST)


def history():
    """40 pre-signal trading bars, one red -7% signal and a dual-hit next day."""
    stock, index = {}, {}
    d = date(2026, 9, 15)
    while d <= date(2026, 11, 24):
        if d.weekday() < 5:
            date_key = d.isoformat()
            stock[date_key] = {"Open": 100, "High": 101, "Low": 99,
                               "Close": 100, "Volume": 1000}
            index[date_key] = {"Open": 105, "High": 106, "Low": 104,
                               "Close": 105, "Volume": 2000}
        d += timedelta(days=1)
    stock["2026-11-20"] = {"Open": 99, "High": 100, "Low": 92,
                            "Close": 93, "Volume": 1200}
    stock["2026-11-23"] = {"Open": 92, "High": 104, "Low": 83,
                            "Close": 103, "Volume": 1300}
    stock["2026-11-24"] = {"Open": 104, "High": 108, "Low": 99,
                            "Close": 105, "Volume": 1400}
    return stock, index


def fetcher(source_stock, source_index):
    async def fetch(ticker, as_of):
        assert ticker == "010060"
        return ({d: v for d, v in source_stock.items() if d <= as_of},
                {d: v for d, v in source_index.items() if d <= as_of})
    return fetch


@pytest.mark.asyncio
async def test_blind_study_frozen_forward_and_conservative_daily_fills(tmp_path):
    study = BlindStudy(tmp_path / "oci.sqlite")
    with pytest.raises(ValueError, match="FUTURE"):
        study.initialize(policy(), now=clock(20))
    initial = study.initialize(policy(), now=clock(19))
    assert initial["status"] == "PREPARED_NOT_RUNNING" and not initial["live_orders"]
    assert study.status()["recorded_sessions"] == 0
    assert study.status()["estimated_net_return_pct"] is None

    stock, index = history()
    provider = fetcher(stock, index)
    day1 = await study.step("2026-11-20", now=clock(20), fetcher=provider)
    assert [a["type"] for a in day1["actions"]] == ["PAPER_SIGNAL_NEXT_SESSION_OPEN"]
    assert day1["position"] is None and day1["pending"]["signal_date"] == "2026-11-20"
    assert day1["prism_market_state"] == "UPTREND"
    with pytest.raises(ValueError, match="ALREADY_PROCESSED"):
        await study.step("2026-11-20", now=clock(20), fetcher=provider)

    day2 = await study.step("2026-11-23", now=clock(23), fetcher=provider)
    kinds = [action["type"] for action in day2["actions"]]
    assert kinds == ["PAPER_BUY_NEXT_OPEN", "PAPER_SELL_DAILY_ESTIMATE"]
    assert day2["actions"][1]["reason"] == "STOP_FIRST_OR_COLLISION"
    assert day2["actions"][1]["net_pct"] < -5
    assert day2["position"] is None and day2["pending"] is None
    state = study.status()
    assert state["recorded_sessions"] == 2
    assert state["cash"] < 1_000_000
    assert state["completed_paper_trades"] == 1
    assert state["wins"] == 0 and state["losses"] == 1
    assert state["estimated_net_return_pct"] < 0
    assert state["estimated_max_drawdown_pct"] > 0
    assert state["audit_head_sha256"] == day2["audit_head_sha256"]

    with sqlite3.connect(study.path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="IMMUTABLE"):
            conn.execute("DELETE FROM blind_events WHERE id=1")
        with pytest.raises(sqlite3.DatabaseError, match="IMMUTABLE"):
            conn.execute("UPDATE blind_events SET date='2000-01-01' WHERE id=1")


@pytest.mark.asyncio
async def test_blind_rejects_backfill_missing_data_future_and_changed_code(tmp_path, monkeypatch):
    stock, index = history()
    study = BlindStudy(tmp_path / "oci.sqlite")
    study.initialize(policy(), now=clock(19))
    with pytest.raises(ValueError, match="AFTER_1610"):
        await study.step("2026-11-20", now=clock(20, hour=15), fetcher=fetcher(stock, index))
    with pytest.raises(ValueError, match="AFTER_1610"):
        await study.step("2026-11-20", now=clock(23), fetcher=fetcher(stock, index))

    async def future_bars(ticker, as_of):
        return stock, index

    with pytest.raises(ValueError, match="FUTURE_BAR"):
        await study.step("2026-11-20", now=clock(20), fetcher=future_bars)

    async def unavailable(ticker, as_of):
        raise RuntimeError("private provider failure details")

    with pytest.raises(ValueError, match="PRISM_MCP_DAILY_SOURCE_UNAVAILABLE"):
        await study.step("2026-11-20", now=clock(20), fetcher=unavailable)
    stale_index = {k: v for k, v in index.items() if k != "2026-11-20"}
    with pytest.raises(MarketDataUnavailable, match="NOT_CONFIRMED"):
        await study.step("2026-11-20", now=clock(20),
                         fetcher=fetcher(stock, stale_index))
    assert study.status()["recorded_sessions"] == 0

    # Skipping a real trading session is a blind-data gap, not an opportunity
    # to query past days with later knowledge and claim an on-time decision.
    with pytest.raises(ValueError, match="MISSED_SESSION"):
        await study.step("2026-11-24", now=clock(24), fetcher=fetcher(stock, index))

    # The KOSPI trading calendar catches a missing OCI bar between recorded
    # observations even when OCI's own provider silently skips it.
    missing_oci = {k: v for k, v in stock.items() if k != "2026-11-23"}
    with pytest.raises(ValueError, match="MISSED_SESSION"):
        await study.step("2026-11-24", now=clock(24), fetcher=fetcher(missing_oci, index))
    await study.step("2026-11-20", now=clock(20), fetcher=fetcher(stock, index))
    with pytest.raises(ValueError, match="MISSED_SESSION"):
        await study.step("2026-11-24", now=clock(24), fetcher=fetcher(stock, index))

    monkeypatch.setattr("swing_insight.blind._code_hash", lambda: "tampered-version")
    with pytest.raises(ValueError, match="CODE_CHANGED"):
        study.status()


@pytest.mark.asyncio
async def test_blind_rejects_retrospective_signal_when_no_future_evidence(tmp_path):
    study = BlindStudy(tmp_path / "oci.sqlite")
    study.initialize(policy(), now=clock(19))
    stock, index = history()
    # T-close source should not be upgraded to a T+1 fill. Source only
    # supplies T candle on 11/20, and paper shares remain zero until 11/23.
    result = await study.step("2026-11-20", now=clock(20),
                              fetcher=fetcher(stock, index))
    assert not any("BUY" in action["type"] for action in result["actions"])
    assert result["cash"] == 1_000_000


def test_blind_policy_rejects_weekend_and_invalid_risk():
    with pytest.raises(ValueError, match="WEEKDAY"):
        BlindPolicy("2026-11-21", 1_000_000, 5, 8, 3, 10, 20, 10)
    with pytest.raises(ValueError, match="Invalid"):
        BlindPolicy("2026-11-20", 1_000_000, 5, 8, 4, 10, 20, 10)
