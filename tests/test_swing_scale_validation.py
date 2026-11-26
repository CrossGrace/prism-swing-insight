from pathlib import Path

import pytest

from tools import replay_swing_scale_validation as validation


def _ledger(returns):
    initial = cash = 10_000_000
    curve, trades = [], []
    for day, fraction in returns:
        net = cash * fraction
        cash += net
        trades.append({"signal_date": day, "net_krw": net})
        curve.append({"date": day, "equity": cash})
    return {
        "initial_cash": initial, "ending_cash": cash,
        "equity": curve, "trades": trades, "trade_count": len(trades),
        "wins": sum(fraction > 0 for day, fraction in returns),
        "fill_count": 2 * len(trades), "net_pct": 100 * (cash / initial - 1),
    }


def test_linked_fold_drawdown_tracks_prior_fold_peak():
    first = _ledger([("2025-01-03", 0.10)])
    second = _ledger([("2025-05-07", -0.05)])
    linked = validation.combine([first, second], "candidate")
    assert linked["net_pct"] == pytest.approx(4.5)
    assert linked["mdd_pct"] == pytest.approx(5.0)
    assert linked["win_pct"] == 50


def test_holdout_large_apparent_win_with_one_episode_is_unverified():
    base = _ledger([("2025-09-03", -0.02)])
    candidate = _ledger([("2025-09-03", 0.06)])
    result = validation.holdout_verdict(candidate, base)
    assert result["status"] == "NO_VERIFIED_WINNER"
    assert result["delta_pp"] == pytest.approx(8)
    assert result["delta_without_top_event_pp"] == 0
    assert result["min_detectable_difference_pp"] is None


def test_holdout_gate_checks_paired_sample_and_top_event():
    days = [f"2025-09-{i:02}" for i in range(1, 21)]
    base = _ledger([(day, 0.002) for day in days])
    candidate = _ledger([(day, 0.004) for day in days])
    result = validation.holdout_verdict(candidate, base)
    assert result["status"] == "VERIFIED_RESEARCH_WINNER"
    assert all(result["checks"].values())
    assert len(result["paired_dates"]) == 20
    assert result["delta_without_top_event_pp"] < result["delta_pp"]
    assert result["min_detectable_difference_pp"] == pytest.approx(0, abs=1e-12)


def test_holdout_duplicate_output_guard_before_data_access(tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_text('{"prior": true}')
    monkeypatch.setattr(validation, "read_lock", lambda path: (b"", {}))
    with pytest.raises(FileExistsError, match="HOLDOUT_OUTPUT_ALREADY_EXISTS"):
        validation.run_holdout(Path("/unavailable"), output)
    assert output.read_text() == '{"prior": true}'


def test_shortlist_rejects_tampering(tmp_path):
    changed = tmp_path / "changed.json"
    changed.write_text("{}")
    with pytest.raises(ValueError, match="EXIT_STUDY_RESULT_HASH_MISMATCH"):
        validation.read_ranked(changed)


def test_holdout_only_replays_the_locked_pair(tmp_path, monkeypatch):
    import gzip
    import json
    from dataclasses import asdict
    from hashlib import sha256

    start, end = validation.HOLDOUT
    payload = gzip.compress(json.dumps({"stock": {start: {}}, "index": {start: {}}}).encode())
    snapshot = tmp_path / f"{validation.PREFIX}holdout.json.gz"
    snapshot.write_bytes(payload)
    monkeypatch.setattr(validation, "HOLDOUT_SHA256", sha256(payload).hexdigest())
    base = validation.ScalePolicy()
    candidate = validation.ScalePolicy(entry_parts=7, entry_mode="confirmed")
    lock = {
        "input_sha256": {"holdout": sha256(payload).hexdigest()},
        "baseline": {"policy_id": "baseline", "scale": asdict(base)},
        "candidate": {"policy_id": "chosen", "scale": asdict(candidate)},
    }
    monkeypatch.setattr(validation, "read_lock", lambda path: (b"locked", lock))
    monkeypatch.setattr(validation, "window_data", lambda folder, last: {"stock": {}, "index": {}})
    seen = []

    def fake_replay(*, stock, index, start, end, scale):
        seen.append((start, end, scale))
        return _ledger([])

    monkeypatch.setattr(validation, "replay_account", fake_replay)
    output = tmp_path / "output.json"
    validation.run_holdout(tmp_path, output)
    result = json.loads(output.read_bytes())
    assert seen == [(start, end, base), (start, end, candidate)]
    assert result["verdict"]["status"] == "NO_VERIFIED_WINNER"
    with pytest.raises(FileExistsError):
        validation.run_holdout(tmp_path, output)
    assert len(seen) == 2
