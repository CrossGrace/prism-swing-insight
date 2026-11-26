"""Offline contract tests. Never load broker agents or make network calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from swing_insight.analyst import ThesisOpinion, analyze_thesis, validate_opinion
from swing_insight.journal import PaperPolicy, SwingJournal
from swing_insight.market import MarketDataUnavailable, evaluate_market, normalized_bars
from swing_insight.simulator import RecoveryPolicy, ReplayPolicy, _rsi, replay_ticker
from swing_insight.sources import (get_market_snapshots, get_public_research_ohlcv,
                                   get_public_research_kospi)
from swing_insight.watchlist import Evidence, WatchItem, load_watchlist

AS_OF = "2026-11-19"


def payload(*, declining=False, missing_volume=False, stale=False):
    last = date.fromisoformat(AS_OF) - (timedelta(days=20) if stale else timedelta())
    result = {}
    for n in range(80):
        day = last - timedelta(days=79 - n)
        close = float(100 + n)
        if declining and n == 79:
            close = 95.0  # >10% from latest peak, correction even without distribution days
        result[day.isoformat()] = {"Close": close,
                                  "Volume": None if missing_volume else float(1000 + n)}
    return result


def item(*, thesis="영업현금흐름의 개선이 지속될 것이다.", evidence=True):
    sources = ((Evidence("filing-1", "DART", "2026-11-18", "확인된 공시 사실",
                         "https://dart.fss.or.kr/example"),) if evidence else ())
    return WatchItem("005930", "테스트기업", thesis, ("현금흐름 악화",), sources)


def market(*, correction=False):
    return evaluate_market(ticker="005930", as_of=AS_OF, stock=payload(),
                           index=payload(declining=correction))


def opinion():
    return ThesisOpinion("INTACT", "기존 근거를 검토했습니다.", "반대 근거도 있습니다.",
                         ("다음 실적",), ("filing-1",))


def test_market_reuses_prism_market_pulse_and_blocks_correction():
    calm = market()
    assert calm.index_state == "UPTREND" and calm.new_entries_allowed
    assert calm.stock_ma20 is not None
    severe = market(correction=True)
    assert severe.index_state == "CORRECTION"
    assert not severe.new_entries_allowed
    assert "MARKET_CORRECTION" in severe.blocking_reasons


def test_market_missing_and_stale_data_fail_closed():
    for stock in ({"error": "timeout"}, payload(missing_volume=True), payload(stale=True)):
        observed = evaluate_market(ticker="005930", as_of=AS_OF, stock=stock, index=payload())
        assert not observed.new_entries_allowed
        assert observed.source_status == "MISSING"
    with pytest.raises(MarketDataUnavailable, match="INSUFFICIENT"):
        normalized_bars({"2026-11-19": {"Close": 100, "Volume": 1}}, as_of=AS_OF)
    assert normalized_bars({**payload(), "2026-11-20": {"Close": 99999, "Volume": 1}},
                           as_of=AS_OF)[-1].close == 179.0


def test_market_requires_bar_completed_on_decision_date():
    observed = evaluate_market(ticker="005930", as_of=AS_OF,
                               stock=payload(stale=True), index=payload())
    assert not observed.new_entries_allowed


def test_watchlist_manual_universe_and_provenance(tmp_path):
    config = {"watchlist": [{"ticker": "005930", "company": "테스트", "thesis": "근거",
                             "invalidation": ["반례"],
                             "evidence": [{"id": "a", "source": "DART", "as_of": "2026-11-18",
                                           "fact": "실제 공시", "url": "https://dart.fss.or.kr/test"}]}]}
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    assert load_watchlist(path)[0].ticker == "005930"
    config["watchlist"] *= 3
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="1 or 2"):
        load_watchlist(path)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.body))


@pytest.mark.asyncio
async def test_analyst_rejects_fabricated_citations():
    fake = FakeResponse({"thesis_status": "INTACT", "summary": "근거가 유지됩니다.",
                         "counter_argument": "반박", "monitor_conditions": [],
                         "evidence_ids": ["made-up"]})
    result = await analyze_thesis(item(), market(), model="test-model", client=SimpleNamespace(responses=fake))
    assert result.thesis_status == "UNKNOWN"
    assert fake.calls and fake.calls[0]["text"]["format"]["strict"]


@pytest.mark.asyncio
async def test_analyst_requires_fundamental_sources_even_with_news():
    fake = FakeResponse({"thesis_status": "INTACT", "summary": "신문에 따르면 급등합니다.",
                         "counter_argument": "반박", "monitor_conditions": [],
                         "evidence_ids": []})
    result = await analyze_thesis(item(evidence=False), market(), model="test-model",
                                  news_titles="급등 뉴스", client=SimpleNamespace(responses=fake))
    assert result.thesis_status == "UNKNOWN" and not fake.calls


def test_analyst_validates_strict_evidence():
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        validate_opinion({"thesis_status": "INTACT", "summary": "설명",
                          "counter_argument": "반박", "monitor_conditions": [],
                          "evidence_ids": []}, known_ids={"filing-1"})
    assert opinion().thesis_status == "INTACT"


@pytest.mark.asyncio
async def test_mcp_adapter_reuses_exact_existing_tools(monkeypatch):
    calls = []

    async def fake_call(server, tool, kwargs):
        calls.append((server, tool, kwargs))
        return payload()

    monkeypatch.setattr("swing_insight.sources.native_call", fake_call)
    stock, index = await get_market_snapshots("005930", AS_OF)
    assert len(stock) == 80 and len(index) == 80
    assert [x[1] for x in calls] == ["get_stock_ohlcv", "get_index_ohlcv"]
    assert calls[0][2]["ticker"] == "005930" and calls[1][2]["ticker"] == "1001"


def test_review_append_only_and_version_detection(tmp_path):
    j = SwingJournal(tmp_path / "paper.sqlite")
    first = j.append_review(item(), market(), opinion())
    second = j.append_review(item(), market(), opinion())
    assert second["review_id"] > first["review_id"]
    assert not second["thesis_changed"] and not second["opinion_changed"]
    third = j.append_review(item(thesis="새로운 가설"), market(),
                            replace(opinion(), thesis_status="WATCH"))
    assert third["thesis_changed"] and third["opinion_changed"]
    assert j.latest_review("005930")["review_id"] == third["review_id"]


def test_paper_buy_and_sell_correction_and_idempotency(tmp_path):
    j = SwingJournal(tmp_path / "paper.sqlite")
    j.initialize_paper(PaperPolicy(10000, 0.5, 0.7, 10, 20))
    j.append_review(item(), market(), opinion())
    first = j.paper_fill(ticker="005930", side="BUY", shares=10, as_of=AS_OF,
                         request_id="order-1", current_tickers={"005930"})
    assert first["status"] == "PAPER_FILLED_AT_REFERENCE_CLOSE"
    assert first["reference_close"] == 179
    assert j.paper_fill(ticker="005930", side="BUY", shares=10, as_of=AS_OF,
                        request_id="order-1", current_tickers={"005930"})["status"] == "ALREADY_RECORDED"
    with pytest.raises(ValueError, match="Idempotency"):
        j.paper_fill(ticker="005930", side="BUY", shares=11, as_of=AS_OF,
                     request_id="order-1", current_tickers={"005930"})
    assert j.paper_snapshot()["holdings"] == {"005930": 10}
    assert j.paper_snapshot()["return_pct"] < 0  # fees are debited, not ignored
    j.append_review(item(), market(correction=True), opinion())
    with pytest.raises(ValueError, match="MARKET"):
        j.paper_fill(ticker="005930", side="BUY", shares=1, as_of=AS_OF,
                     request_id="order-2", current_tickers={"005930"})
    assert j.paper_fill(ticker="005930", side="SELL", shares=10, as_of=AS_OF,
                        request_id="order-3", current_tickers={"005930"})["status"] == "PAPER_FILLED_AT_REFERENCE_CLOSE"
    assert j.paper_snapshot()["holdings"] == {}


def test_paper_buy_fails_closed_without_llm_or_budget(tmp_path):
    j = SwingJournal(tmp_path / "paper.sqlite")
    with pytest.raises(ValueError, match="Initialize"):
        j.paper_snapshot()
    with pytest.raises(ValueError, match="exposure"):
        PaperPolicy(10000, 0.8, 0.5, 10, 20)
    j.initialize_paper(PaperPolicy(10000, 0.1, 0.2, 10, 20))
    j.append_review(item(), market(), replace(opinion(), thesis_status="UNKNOWN"))
    with pytest.raises(ValueError, match="FUNDAMENTAL"):
        j.paper_fill(ticker="005930", side="BUY", shares=1, as_of=AS_OF,
                     request_id="order-1", current_tickers={"005930"})
    j.append_review(item(), market(), opinion())
    with pytest.raises(ValueError, match="EXPOSURE"):
        j.paper_fill(ticker="005930", side="BUY", shares=10, as_of=AS_OF,
                     request_id="order-2", current_tickers={"005930"})


def replay_fixture():
    """Construct 80 sessions: a sharp dip/reversal and a stop/take collision."""
    first = date(2026, 7, 1)
    stock, index = {}, {}
    days = []
    for i in range(80):
        day = (first + timedelta(days=i)).isoformat()
        days.append(day)
        close = {72: 95, 73: 91, 74: 92}.get(i, 100.0)
        stock[day] = {"Open": close, "High": close + 1, "Low": close - 1,
                      "Close": close, "Volume": 1000}
        index[day] = {"Open": 100 + i, "High": 102 + i, "Low": 99 + i,
                      "Close": 100 + i, "Volume": 1000}
    # Entry price 92; the same intraday candle touches both 3% stop and 5% take.
    stock[days[75]] = {"Open": 92, "High": 99, "Low": 87, "Close": 98, "Volume": 1000}
    return stock, index, days


def test_replay_rsi_uses_wilder_smoothed_gains_losses():
    assert _rsi([100] * 15) == 50.0
    assert _rsi(list(range(15))) == 100.0
    # 14 consecutive one-point gains seed avgGain=1, avgLoss=0;
    # a subsequent one-point loss gives Wilder avgGain=13/14,
    # avgLoss=1/14, therefore RSI=92.857... (not Cutler RSI).
    assert _rsi(list(range(15)) + [13]) == pytest.approx(100 * 13 / 14)


def test_replay_next_open_and_conservative_stop_collision():
    stock, index, days = replay_fixture()
    test_item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), (
        Evidence("lta", "OCI IR", days[70], "공급 계약", "https://example.org/source"),))
    policy = ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 0)
    result = replay_ticker(item=test_item, stock=stock, index=index,
                           start=days[40], end=days[-1], policy=policy)
    t = next(t for t in result["trades"] if t["signal_date"] == days[74])
    assert t["entry_date"] == days[75]
    assert t["exit_date"] == days[75] and t["hold_days"] == 0
    assert t["exit_reason"] == "STOP_FIRST_OR_COLLISION"
    assert t["net_return_pct"] < -3
    assert result["open_position"] is None
    assert result["marked_net_sleeve_pct"] == result["compounded_net_sleeve_pct"]


def test_replay_keeps_final_session_entry_open_and_marks_its_value():
    stock, _, days = replay_fixture()
    stock[days[75]] = {"Open": 92, "High": 94, "Low": 91, "Close": 93, "Volume": 1000}
    item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), ())
    policy = ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 10)

    result = replay_ticker(item=item, stock=stock, index=None, start=days[40],
                           end=days[75], policy=policy, price_only=True, assume_uptrend=True)

    assert result["trades"] == []
    assert result["count"] == 0 and result["win_rate_pct"] is None
    assert result["compounded_net_sleeve_pct"] == 0
    position = result["open_position"]
    assert position["signal_date"] == days[74]
    assert position["entry_date"] == position["mark_date"] == days[75]
    assert position["entry_price"] == pytest.approx(92 * 1.001)
    assert position["mark_close"] == 93
    expected = 100 * (93 * 0.999 * 0.997 / (92 * 1.001 * 1.001) - 1)
    assert position["marked_net_return_pct"] == round(expected, 4)
    assert result["marked_net_sleeve_pct"] == round(expected, 3)


def test_replay_fundamental_date_gate_and_missing_ohlc_fail_closed():
    stock, index, days = replay_fixture()
    policy = ReplayPolicy(90, 2, 3, 5, 0, 10, 20, 5)
    late = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), (
        Evidence("late", "IR", days[-1], "계약", "https://example.org/source"),))
    result = replay_ticker(item=late, stock=stock, index=index,
                           start=days[40], end=days[-1], policy=policy)
    assert result["trades"] == [] and result["win_rate_pct"] is None
    stock[days[75]].pop("Low")
    with pytest.raises(MarketDataUnavailable, match="MISSING_OHLC"):
        replay_ticker(item=late, stock=stock, index=index,
                      start=days[40], end=days[-1], policy=policy)


def test_replay_kospi_correction_veto_visible_in_result():
    stock, index, days = replay_fixture()
    index[days[74]] = {"Open": 95, "High": 96, "Low": 94,
                       "Close": 95, "Volume": 1000}
    test_item = WatchItem("010120", "LS ELECTRIC", "LTA", ("계약 악화",), (
        Evidence("lta", "IR", days[70], "공급 계약", "https://example.org/source"),))
    result = replay_ticker(item=test_item, stock=stock, index=index,
                           start=days[40], end=days[-1],
                           policy=ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 0))
    assert not any(t["signal_date"] == days[74] for t in result["trades"])
    assert any("MARKET_CORRECTION" in event["reason"]
               for event in result["market_guard_rejections"])


def test_replay_explicit_bullish_assumption_works_without_index_data():
    stock, _, days = replay_fixture()
    test_item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), ())
    result = replay_ticker(item=test_item, stock=stock, index=None,
                           start=days[70], end=days[-1], price_only=True,
                           assume_uptrend=True,
                           policy=ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 0))
    assert any(t["signal_date"] == days[74] for t in result["trades"])
    assert result["market_guard"] == "ASSUMED_UPTREND_NOT_VERIFIED"
    assert result["compounded_net_sleeve_pct"] > 0 or result["count"] >= 1


def test_replay_tactical_correction_keeps_prism_state_and_limits_position():
    stock, index, days = replay_fixture()
    for i, day in enumerate(days):
        index[day]["Close"] = 100 - 0.5 * i  # correction, but no one-day panic
    test_item = WatchItem("010060", "OCI홀딩스", "LTA", ("악화",), ())
    policy = ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 0)
    strict = replay_ticker(item=test_item, stock=stock, index=index,
                           start=days[40], end=days[-1], policy=policy, price_only=True)
    tactical = replay_ticker(item=test_item, stock=stock, index=index,
                             start=days[40], end=days[-1], policy=policy,
                             price_only=True, market_mode="tactical")
    assert strict["count"] == 0
    trade = next(t for t in tactical["trades"] if t["signal_date"] == days[74])
    assert trade["market_state"] == "CORRECTION"
    assert trade["position_fraction_of_sleeve"] == 0.25
    assert tactical["compounded_net_sleeve_pct"] < 0  # stop-filled drawdown scaled by 25%
    assert abs(tactical["compounded_net_sleeve_pct"]) < abs(trade["net_return_pct"])


def test_replay_tactical_vetoes_index_panic_and_conflicting_assumption():
    stock, index, days = replay_fixture()
    for i, day in enumerate(days):
        index[day]["Close"] = 100 - 0.5 * i
    index[days[74]]["Close"] = 45.0  # >4% KOSPI collapse on signal day
    test_item = WatchItem("010060", "OCI홀딩스", "LTA", ("악화",), ())
    policy = ReplayPolicy(90, 2, 3, 5, 3, 10, 20, 0)
    tactical = replay_ticker(item=test_item, stock=stock, index=index,
                             start=days[40], end=days[-1], policy=policy,
                             price_only=True, market_mode="tactical")
    assert not tactical["trades"]
    assert any("KOSPI_PANIC" in e["reason"] for e in tactical["market_guard_rejections"])
    with pytest.raises(ValueError, match="conflicting"):
        replay_ticker(item=test_item, stock=stock, index=index,
                      start=days[40], end=days[-1], policy=policy, price_only=True,
                      assume_uptrend=True, market_mode="tactical")


@pytest.mark.asyncio
async def test_public_daily_research_source_trims_old_bars_and_blocks_splits(monkeypatch):
    stock, _, days = replay_fixture()
    rows = [[d.replace("-", ""), v["Close"], v["Volume"], v["Open"], v["High"], v["Low"]]
            for d, v in stock.items()]
    raw = {"code": "010060", "as_of": days[-1].replace("-", ""),
           "columns": ["date", "close", "volume", "open", "high", "low"],
           "rows": rows, "breaks": []}

    async def fake_thread(fn):
        return raw

    monkeypatch.setattr("swing_insight.sources.asyncio.to_thread", fake_thread)
    fetched = await get_public_research_ohlcv("010060", days[-1], start=days[35])
    assert fetched[days[-1]]["Close"] == stock[days[-1]]["Close"]
    raw["breaks"] = [{"date": days[33].replace("-", "") }]
    with pytest.raises(ValueError, match="CORPORATE_ACTION"):
        await get_public_research_ohlcv("010060", days[-1], start=days[35])


@pytest.mark.asyncio
async def test_public_kospi_daily_source_asof_and_volume_contract(monkeypatch):
    raw = {"code": "KOSPI", "periodType": "dayCandle", "hasVolume": True,
           "priceInfos": [
               {"localDate": "20261117", "openPrice": 5008.09, "highPrice": 5084.06,
                "lowPrice": 4957.0, "closePrice": 4968.48,
                "accumulatedTradingVolume": 193980000},
               {"localDate": "20261118", "openPrice": 5100, "highPrice": 5200,
                "lowPrice": 4900, "closePrice": 5150,
                "accumulatedTradingVolume": 200000000},
           ]}

    async def fake_thread(fn):
        return raw

    monkeypatch.setattr("swing_insight.sources.asyncio.to_thread", fake_thread)
    result = await get_public_research_kospi("2026-11-17")
    assert list(result) == ["2026-11-17"]
    assert result["2026-11-17"]["Close"] == 4968.48
    assert result["2026-11-17"]["Volume"] == 193980000
    raw["hasVolume"] = False
    with pytest.raises(ValueError, match="SCHEMA_CHANGED"):
        await get_public_research_kospi("2026-11-17")


def test_replay_requires_explicit_valid_costs_and_intraday_limit():
    with pytest.raises(ValueError, match="Invalid replay policy"):
        ReplayPolicy(90, 2, 3, 5, 4, 10, 20, 5)
    with pytest.raises(ValueError, match="Invalid replay policy"):
        ReplayPolicy(90, 2, 3, 5, 1, -10, 20, 5)


def test_recovery_waits_two_sessions_and_trails_remaining_half_after_close():
    stock, _, days = replay_fixture()
    stock[days[74]].update(Open=91, High=94, Low=88, Close=89)
    stock[days[75]].update(Open=90, High=93, Low=89, Close=92)
    stock[days[76]].update(Open=94, High=103, Low=93, Close=101)
    stock[days[77]].update(Open=100, High=106, Low=98, Close=104)
    stock[days[78]].update(Open=101, High=103, Low=98, Close=99)
    item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), ())
    policy = ReplayPolicy(90, 2, 3, 5, 3, 0, 0, 0)
    args = dict(item=item, stock=stock, index=None, start=days[74], policy=policy,
                price_only=True, assume_uptrend=True, recovery=RecoveryPolicy())

    result = replay_ticker(**args, end=days[-1])
    trade, = result["trades"]
    assert trade["signal_date"] == days[74]
    assert trade["entry_date"] == days[76]
    assert trade["entry_price"] == 94
    first, last = trade["exit_legs"]
    assert (first["exit_date"], first["exit_reason"], first["fraction"]) == (days[76], "PARTIAL_TAKE", 0.5)
    assert (last["exit_date"], last["exit_reason"], last["fraction"]) == (days[78], "TRAIL_STOP", 0.5)
    assert last["exit_price"] == pytest.approx(104 * 0.95)
    expected = 100 * ((94 * 1.05 + 104 * 0.95) / (2 * 94) - 1)
    assert trade["net_return_pct"] == round(expected, 4)

    prefix = replay_ticker(**args, end=days[76])
    assert prefix["count"] == 0
    assert prefix["open_position"]["exit_legs"] == [first]
    assert prefix["open_position"]["remaining_fraction"] == 0.5


def test_recovery_does_not_assume_intraday_low_preceded_breakout():
    stock, _, days = replay_fixture()
    stock[days[74]].update(Open=91, High=94, Low=88, Close=89)
    stock[days[75]].update(Open=89, High=110, Low=86, Close=105)
    item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), ())
    policy = ReplayPolicy(90, 2, 3, 5, 3, 0, 0, 0)
    result = replay_ticker(item=item, stock=stock, index=None, start=days[74],
                           end=days[75], policy=policy, price_only=True,
                           assume_uptrend=True, recovery=RecoveryPolicy())
    trade, = result["trades"]
    assert trade["entry_price"] == 94
    assert trade["exit_reason"] == "ENTRY_BAR_STOP_ASSUMED"
    assert trade["net_return_pct"] == -3

    # A gap-entry variant uses the observed open and never reads the day's low
    # to decide whether to enter. Its stop and target apply after the opening fill.
    stock[days[75]].update(Open=87, Low=86.5)
    gap = replay_ticker(item=item, stock=stock, index=None, start=days[74],
                        end=days[75], policy=policy, price_only=True,
                        assume_uptrend=True, recovery=RecoveryPolicy(gap_entry=True))
    position = gap["open_position"]
    assert position["entry_basis"] == "RECOVERY_GAP"
    assert position["entry_price"] == 87
    assert position["exit_legs"][0]["exit_reason"] == "PARTIAL_TAKE"
    assert position["remaining_fraction"] == 0.5


@pytest.mark.parametrize("recovery", [None, RecoveryPolicy(), RecoveryPolicy(gap_entry=True)])
def test_blind_replay_prefix_is_independent_of_future_bars(recovery):
    stock, _, days = replay_fixture()
    start = days[74] if recovery else days[40]
    if recovery:
        stock[days[74]].update(Open=91, High=94, Low=88, Close=89)
        stock[days[75]].update(Open=87 if recovery.gap_entry else 95, High=110, Low=86 if recovery.gap_entry else 94, Close=103)
    item = WatchItem("010060", "OCI홀딩스", "LTA", ("계약 악화",), ())
    args = dict(item=item, index=None, start=start, end=days[75],
                policy=ReplayPolicy(90, 2, 3, 5, 3, 0, 0, 0),
                price_only=True, assume_uptrend=True, recovery=recovery)
    prefix = {day: row for day, row in stock.items() if day <= days[75]}
    # This malformed future bar would throw if any strategy tried to read it.
    future = {**stock, days[76]: {"Open": "FUTURE"}}
    result = replay_ticker(**args, stock=prefix)
    assert result == replay_ticker(**args, stock=future)
    assert result["execution"] == "BLIND_CHRONOLOGICAL_DAILY_REPLAY"
    assert result["trades"] or result["open_position"]
