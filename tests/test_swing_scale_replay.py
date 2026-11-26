"""Offline fixtures enforce point-in-time and paper-account constraints."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from swing_insight.scale_replay import ScalePolicy, replay_account
from tools.replay_swing_scale import window_data


def _bar(o, h, l, c):
    return dict(Open=o, High=h, Low=l, Close=c, Volume=10000)


def _history(price=100):
    first = date(2024, 1, 2)
    days = [(first + timedelta(days=i)).isoformat() for i in range(60)
            if (first + timedelta(days=i)).weekday() < 5]
    market = {day: _bar(100 + i, 102 + i, 99 + i, 101 + i) for i, day in enumerate(days)}
    stock = {day: _bar(price, price * 1.01, price * 0.99, price) for day in days}
    return days, stock, market


def _replay(days, stock, market, **kwargs):
    return replay_account(stock=stock, index=market, start=days[30], end=days[-1], **kwargs)


def test_reject_future_prices_and_missing_sessions():
    days, stock, market = _history()
    stock["2100-01-01"] = object()
    with pytest.raises(ValueError, match="FUTURE_BAR"):
        _replay(days, stock, market)
    del stock["2100-01-01"]
    del market[days[30]]
    with pytest.raises(ValueError, match="SESSION_MISMATCH"):
        _replay(days, stock, market)


def test_later_mutation_cannot_change_earlier_signals_or_fills():
    days, stock, market = _history()
    stock[days[30]] = _bar(99, 100, 91, 92)
    stock[days[31]] = _bar(89, 100, 80, 97)  # Both thresholds reached.
    original = _replay(days, stock, market)
    stock[days[35]] = _bar(99, 100, 69, 70)
    market[days[35]] = _bar(88, 94, 86, 89)
    changed = _replay(days, stock, market)
    trade = original["trades"][0]
    assert trade == changed["trades"][0]
    assert trade["signal_date"] == days[30] and trade["entry_date"] == days[31]
    assert [fill["reason"] for fill in trade["fills"]] == ["RECOVERY_GAP", "STOP_FIRST"]
    assert trade["net_krw"] < 0


def test_seven_integer_adds_before_hit_stop_and_self_financing():
    days, stock, market = _history()
    stock[days[30]] = _bar(99, 100, 91, 92)
    stock[days[31]] = _bar(89, 90, 88, 89.5)
    stock[days[32]] = _bar(89, 98, 85, 88)
    result = _replay(days, stock, market, scale=ScalePolicy(entry_parts=7, exit_parts=7))
    trade = result["trades"][0]
    fills = trade["fills"]
    assert [fill["side"] for fill in fills] == ["BUY"] * 7 + ["SELL"]
    assert fills[-1]["reason"] == "STOP_FIRST"
    assert all(type(fill["shares"]) is int and fill["shares"] > 0 for fill in fills)
    assert all(fill["cash_after"] >= 0 and fill["shares_after"] >= 0 for fill in fills)
    assert fills[-1]["shares_after"] == 0
    assert trade["spent"] <= trade["allocation"]
    assert result["ending_cash"] == pytest.approx(result["initial_cash"] + trade["net_krw"])
    assert result["fees"] > 0 and result["slippage"] > 0 and result["tax"] > 0


def test_opening_targets_and_trail_use_prior_close_with_integer_residual():
    days, stock, market = _history(100_000)
    stock[days[30]] = _bar(99000, 100000, 91000, 92000)
    stock[days[31]] = _bar(89000, 90000, 88000, 89500)
    stock[days[32]] = _bar(98500, 100000, 98000, 99500)
    stock[days[33]] = _bar(90000, 92000, 89000, 90000)
    result = _replay(days, stock, market, scale=ScalePolicy(exit_parts=7, trail_pct=2.5))
    fills = result["trades"][0]["fills"]
    assert [fill["reason"] for fill in fills] == ["RECOVERY_GAP"] + ["PARTIAL_TAKE"] * 6 + ["STOP_GAP"]
    assert sum(fill["shares"] for fill in fills[1:]) == fills[0]["shares"]
    assert result["fully_scaled_exits"] == 1


def test_single_share_targets_record_zero_without_selling_unowned():
    days, stock, market = _history(7_000_000)
    stock[days[30]] = _bar(6_930_000, 7_000_000, 6_370_000, 6_440_000)
    stock[days[31]] = _bar(6_230_000, 7_000_000, 6_160_000, 6_950_000)
    result = _replay(days, stock, market, scale=ScalePolicy(exit_parts=7))
    assert result["zero_share_targets"] == 6
    assert result["buy_count"] == result["sell_count"] == 1


def test_confirmed_adds_fill_only_at_later_open():
    days, stock, market = _history()
    stock[days[30]] = _bar(99, 100, 91, 92)
    stock[days[31]] = _bar(89, 90, 88, 90)
    stock[days[32]] = _bar(89.8, 90, 89.5, 89.9)
    stock[days[33]] = _bar(89.95, 90.1, 89.9, 90)
    stock[days[34]] = _bar(90, 90.2, 89.9, 90)
    result = _replay(days, stock, market, scale=ScalePolicy(entry_parts=7, entry_mode="confirmed"))
    buys = [f for f in result["trades"][0]["fills"] if f["side"] == "BUY"]
    assert buys[0]["date"] == days[31]
    assert all(f["date"] > days[31] and f["reason"] == "ADD_CONFIRMED_OPEN" for f in buys[1:])
    assert 1 < len(buys) <= 4


def test_boundary_embargo_prevents_pending_or_overlapping_holding():
    days, stock, market = _history()
    stock[days[-5]] = _bar(99, 100, 91, 92)
    assert _replay(days, stock, market)["trade_count"] == 0
    with pytest.raises(ValueError, match="FINAL_HOLDOUT_LOCKED"):
        window_data(Path("/should-not-be-read"), "2025-09-01")


def test_entry_grid_is_fixed_and_does_not_change_exit_rules():
    from collections import Counter

    from tools.replay_swing_scale import entry_candidates

    candidates = entry_candidates()
    assert candidates.pop("baseline") == ScalePolicy()
    assert len(candidates) == len(set(candidates.values())) == 32
    assert Counter(policy.entry_parts for policy in candidates.values()) == {2: 8, 3: 8, 5: 8, 7: 8}
    assert all(
        (p.exit_parts, p.exit_gap_pct, p.exit_weights, p.trail_pct) == (2, 0.75, "equal", 5.0)
        for p in candidates.values()
    )


def test_entry_study_ties_use_drawdown_fill_count_then_id():
    from tools.replay_swing_scale import rank_key

    first = dict(net_pct=3, mdd_pct=4, fill_count=7, policy_id="c")
    assert rank_key(first) < rank_key({**first, "net_pct": 2.99})
    assert rank_key(first) < rank_key({**first, "mdd_pct": 4.1})
    assert rank_key(first) < rank_key({**first, "fill_count": 8})
    assert rank_key(first) < rank_key({**first, "policy_id": "d"})
