"""Run the fixed OCI walk-forward comparison or its one-shot locked holdout."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import gzip
from hashlib import sha256
import json
from math import prod, sqrt
import os
from pathlib import Path
from statistics import stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swing_insight.scale_replay import BASELINE, ScalePolicy, replay_account
from tools.replay_swing_scale import DEV, FINGERPRINTS, PREFIX, WALK, rank_key, summarize_exit, window_data

BASE_COMMIT = "4eba79d5ce43d3d531425f1e438f49cf7b2874f8"
EXIT_RESULT_SHA256 = "a4a8b5a0b9c84570ef0bb1f5699bb8edbe881605b98f10853666ef890ca03747"
HOLDOUT = ("2025-09-01", "2026-06-16")
HOLDOUT_SHA256 = "72d6bf8021c67754e49cd3ddb66b75f071d9c451f888688f239486bd0f5caac0"
LOCK_NAME = "docs/PRISM_OCI_BLIND_SCALE_20261127_lock.json"
MIN_PAIRED = 20
SCENARIOS = {
    "base": (BASELINE, 0.0),
    "slip_5bp": (replace(BASELINE, slippage_bps=5), 0.0),
    "slip_25bp": (replace(BASELINE, slippage_bps=25), 0.0),
    "fee_20bp_slip_25bp": (replace(BASELINE, fee_bps=20, slippage_bps=25), 0.0),
    "trade_through_0.1pct": (BASELINE, 0.1),
}


def code_sha256() -> dict[str, str]:
    code = ("tools/replay_swing_scale_validation.py", "tools/replay_swing_scale.py",
            "swing_insight/scale_replay.py", "swing_insight/simulator.py",
            "swing_insight/market.py", "swing_insight/watchlist.py", "cores/market_pulse.py")
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in code}


def read_ranked(path: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != EXIT_RESULT_SHA256:
        raise ValueError("EXIT_STUDY_RESULT_HASH_MISMATCH")
    result = json.loads(raw)
    if result["input_sha256"] != FINGERPRINTS["dev"]:
        raise ValueError("EXIT_STUDY_DEVELOPMENT_INPUT_MISMATCH")
    ranked = [result["baseline"], *result["ranked_exits"][:8]]
    shortlist = result["walk_study_candidates"]
    if shortlist != [{"policy_id": row["policy_id"], "scale": row["scale"]} for row in ranked]:
        raise ValueError("EXIT_STUDY_SHORTLIST_MISMATCH")
    return ({row["policy_id"]: ScalePolicy(**row["scale"]) for row in shortlist},
            {row["policy_id"]: row for row in ranked})


def combine(folds: list[dict], policy_id: str) -> dict:
    equity_factor, peak, drawdown = 1.0, 1.0, 0.0
    for fold in folds:
        for point in fold["equity"]:
            level = equity_factor * point["equity"] / fold["initial_cash"]
            peak = max(peak, level)
            drawdown = max(drawdown, 100 * (1 - level / peak))
        equity_factor *= fold["ending_cash"] / fold["initial_cash"]
    trades = sum(fold["trade_count"] for fold in folds)
    wins = sum(fold["wins"] for fold in folds)
    return {
        "policy_id": policy_id,
        "net_pct": 100 * (equity_factor - 1),
        "mdd_pct": drawdown,
        "trade_count": trades,
        "wins": wins,
        "win_pct": 100 * wins / trades if trades else None,
        "fill_count": sum(fold["fill_count"] for fold in folds),
    }


def _base_train_rank(dev: dict, prior: dict) -> tuple:
    return rank_key(combine([dev, prior], dev["policy_id"]))


def walk_study(snapshot: Path, exit_result: Path) -> dict:
    candidates, dev_ranks = read_ranked(exit_result)
    selection = [key for key in candidates if key != "baseline"]
    first = min(selection, key=lambda key: rank_key(dev_ranks[key]))
    dev_data = window_data(snapshot, DEV[1])
    dev_ledgers = {key: {**replay_account(**dev_data, start=DEV[0], end=DEV[1], scale=candidates[key]),
                         "policy_id": key} for key in selection}

    results = {}
    forward = None
    for scenario, (policy, trade_through) in SCENARIOS.items():
        folds = []
        selected = first
        chosen = []
        for start, end in WALK:
            data = window_data(snapshot, end)
            ledgers = {
                key: replay_account(**data, start=start, end=end, scale=scale,
                                    policy=policy, trade_through_pct=trade_through)
                for key, scale in candidates.items()
            }
            folds.append(ledgers)
            if scenario == "base":
                chosen.append(selected)
                if len(folds) == 1:
                    selected = min(selection, key=lambda key: _base_train_rank(dev_ledgers[key], ledgers[key]))
        scores = [combine([fold[key] for fold in folds], key) for key in candidates]
        reference = scores[0]["net_pct"]
        for score in scores:
            score["delta_pp"] = score["net_pct"] - reference
        results[scenario] = {
            "execution": {"policy": asdict(policy), "trade_through_pct": trade_through},
            "ranked": sorted(scores[1:], key=rank_key),
            "baseline": scores[0],
            "folds": [{key: {**summarize_exit(fold[key], key),
                             "delta_pp": fold[key]["net_pct"] - fold["baseline"]["net_pct"]}
                       for key in candidates} for fold in folds],
        }
        if scenario == "base":
            forward = {
                "ids": chosen,
                "trained_through": [DEV[1], WALK[0][1]],
                "folds": [summarize_exit(fold[key], key) for fold, key in zip(folds, chosen)],
                "linked": combine([fold[key] for fold, key in zip(folds, chosen)], "walk-forward"),
            }

    winner = results["base"]["ranked"][0]["policy_id"]
    lock = {
        "protocol": "PRISM_OCI_BLIND_SCALE_20261127_v1",
        "status": "LOCKED_FOR_HOLDOUT",
        "baseline_commit": BASE_COMMIT,
        "code_sha256": code_sha256(),
        "input_sha256": {**FINGERPRINTS, "holdout": HOLDOUT_SHA256},
        "holdout": list(HOLDOUT),
        "execution": results["base"]["execution"],
        "baseline": {"policy_id": "baseline", "scale": asdict(candidates["baseline"])},
        "candidate": {"policy_id": winner, "scale": asdict(candidates[winner])},
        "min_paired": MIN_PAIRED,
    }
    return {"exit_result_sha256": EXIT_RESULT_SHA256, "walk": results,
            "walk_forward": forward, "lock": lock}


def _episode_returns(ledger: dict) -> dict[str, float]:
    cash = ledger["initial_cash"]
    episodes = {}
    for trade in ledger["trades"]:
        day = trade["signal_date"]
        if day in episodes or cash <= 0:
            raise ValueError("INVALID_EPISODE_LEDGER")
        episodes[day] = trade["net_krw"] / cash
        cash += trade["net_krw"]
    return episodes


def holdout_verdict(candidate: dict, baseline: dict) -> dict:
    selected, reference = _episode_returns(candidate), _episode_returns(baseline)
    paired = sorted(selected.keys() & reference.keys())
    union = sorted(selected.keys() | reference.keys())

    def without(day: str | None) -> float:
        return 100 * (prod(1 + ret for date, ret in selected.items() if date != day)
                      - prod(1 + ret for date, ret in reference.items() if date != day))

    delta = candidate["net_pct"] - baseline["net_pct"]
    # Drop the one signal date that reduces the paired strategy advantage most.
    removed = min(union, key=without) if union else None
    deflated = without(removed)
    paired_sd = stdev(100 * (selected[day] - reference[day]) for day in paired) if len(paired) > 1 else None
    checks = {
        "paired_sample": len(paired) >= MIN_PAIRED,
        "candidate_positive": candidate["net_pct"] > 0,
        "beats_baseline": delta > 0,
        "beats_after_one_event_removed": deflated > 0,
    }
    return {
        "status": "VERIFIED_RESEARCH_WINNER" if all(checks.values()) else "NO_VERIFIED_WINNER",
        "checks": checks, "paired_dates": paired, "paired_min": MIN_PAIRED, "dates_union": union,
        "delta_pp": delta, "removed_signal_date": removed, "delta_without_top_event_pp": deflated,
        "paired_sd_pp": paired_sd,
        "min_detectable_difference_pp": (2.80 * paired_sd / sqrt(len(paired))
                                         if paired_sd is not None else None),
    }


def read_lock(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    lock = json.loads(raw)
    if (lock["protocol"] != "PRISM_OCI_BLIND_SCALE_20261127_v1"
            or lock["status"] != "LOCKED_FOR_HOLDOUT" or lock["code_sha256"] != code_sha256()
            or lock["baseline_commit"] != BASE_COMMIT or lock["holdout"] != list(HOLDOUT)
            or lock["input_sha256"] != {**FINGERPRINTS, "holdout": HOLDOUT_SHA256}
            or lock["min_paired"] != MIN_PAIRED
            or lock["baseline"] != {"policy_id": "baseline", "scale": asdict(ScalePolicy())}
            or lock["candidate"]["policy_id"] == "baseline"
            or lock["execution"] != {"policy": asdict(BASELINE), "trade_through_pct": 0.0}):
        raise ValueError("HOLDOUT_LOCK_OR_CODE_MISMATCH")
    return raw, lock


def run_holdout(snapshot: Path, output: Path) -> None:
    lock_raw, lock = read_lock(ROOT / LOCK_NAME)
    if output.exists():
        raise FileExistsError(f"HOLDOUT_OUTPUT_ALREADY_EXISTS:{output}")
    dev_walk = window_data(snapshot, WALK[-1][1])
    raw = (snapshot / f"{PREFIX}holdout.json.gz").read_bytes()
    if sha256(raw).hexdigest() != HOLDOUT_SHA256:
        raise ValueError("HOLDOUT_INPUT_HASH_MISMATCH")
    test = json.loads(gzip.decompress(raw))
    if any(day < HOLDOUT[0] or day > HOLDOUT[1] for series in test.values() for day in series):
        raise ValueError("HOLDOUT_WINDOW_CONTAMINATED")
    prices = {key: {**dev_walk[key], **test[key]} for key in ("stock", "index")}
    with output.open("x") as result:
        ledgers = {
            lane: replay_account(**prices, start=HOLDOUT[0], end=HOLDOUT[1],
                                 scale=ScalePolicy(**lock[lane]["scale"]))
            for lane in ("baseline", "candidate")
        }
        json.dump({
            "lock_sha256": sha256(lock_raw).hexdigest(), "input_sha256": lock["input_sha256"],
            "verdict": holdout_verdict(ledgers["candidate"], ledgers["baseline"]),
            **{lane: {**ledger, "policy_id": lock[lane]["policy_id"]} for lane, ledger in ledgers.items()},
        }, result, indent=2, allow_nan=False)
        result.write("\n")
        result.flush()
        os.fsync(result.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="stage", required=True)
    walk = command.add_parser("walk")
    walk.add_argument("snapshot", type=Path)
    walk.add_argument("exit_result", type=Path)
    hold = command.add_parser("holdout")
    hold.add_argument("snapshot", type=Path)
    hold.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.stage == "walk":
        print(json.dumps(walk_study(args.snapshot, args.exit_result), indent=2, allow_nan=False))
    else:
        run_holdout(args.snapshot, args.output)


if __name__ == "__main__":
    main()
