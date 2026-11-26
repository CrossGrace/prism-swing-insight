"""Replay the preregistered OCI account baseline and development studies."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
import gzip
from itertools import product
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swing_insight.scale_replay import ScalePolicy, replay_account

DEV = ("2023-10-04", "2024-12-30")
WALK = (("2025-01-02", "2025-04-30"), ("2025-05-02", "2025-08-29"))
FINGERPRINTS = {
    "dev": "7d1848066b8d5182a56ec5dee7f153c3ee12011109e8635696f7b2aefded1243",
    "walk": "2e15afa2a1562e82f8dfffb6065c15539ce6a6ebe091eea25bb6ba21304de479",
}
PREFIX = "PRISM_OCI_BLIND_SCALE_20261127_"
ENTRY_STUDY_SHA256 = "0f144e8cf486d639b660cbd4c424e2b6415d45c02d65d89f18d282e5ab72fe24"


def read_input(directory: Path, name: str) -> dict:
    raw = (directory / f"{PREFIX}{name}.json.gz").read_bytes()
    if sha256(raw).hexdigest() != FINGERPRINTS[name]:
        raise ValueError(f"RESEARCH_INPUT_HASH_MISMATCH:{name}")
    return json.loads(gzip.decompress(raw))


def window_data(directory: Path, end: str) -> dict:
    if end > WALK[-1][1]:
        raise ValueError("FINAL_HOLDOUT_LOCKED")
    dev = read_input(directory, "dev")
    if end <= DEV[1]:
        return {key: {day: row for day, row in values.items() if day <= end}
                for key, values in dev.items()}
    walk = read_input(directory, "walk")
    return {key: {**dev[key], **{day: row for day, row in walk[key].items() if day <= end}}
            for key in ("stock", "index")}


def entry_candidates() -> dict[str, ScalePolicy]:
    candidates = {"baseline": ScalePolicy()}
    for parts, gap, weights, mode in product(
        (2, 3, 5, 7), (0.4, 0.8), ("equal", "front"), ("limit", "confirmed"),
    ):
        policy_id = f"entry-{parts}-{gap:.1f}-{weights}-{mode}"
        candidates[policy_id] = ScalePolicy(
            entry_parts=parts, entry_gap_pct=gap, entry_weights=weights, entry_mode=mode,
        )
    return candidates


def rank_key(candidate: dict) -> tuple:
    return (-candidate["net_pct"], candidate["mdd_pct"], candidate["fill_count"],
            candidate["policy_id"])


def summarize_replay(result: dict, policy_id: str) -> dict:
    trades = result["trades"]
    buys = [fill for trade in trades for fill in trade["fills"] if fill["side"] == "BUY"]
    allocation = sum(trade["allocation"] for trade in trades)
    spent = sum(trade["spent"] for trade in trades)
    return {
        "policy_id": policy_id,
        **{key: value for key, value in result.items() if key not in {"equity", "trades"}},
        "signal_dates": [trade["signal_date"] for trade in trades],
        "market_states": [trade["market_state"] for trade in trades],
        "shares_bought": sum(fill["shares"] for fill in buys),
        "spent_krw": spent,
        "allocation_krw": allocation,
        "used_allocation_pct": 100 * spent / allocation if allocation else None,
        "buy_orders_per_trade": len(buys) / len(trades) if trades else None,
        "fully_scaled_entries_pct": (100 * result["fully_scaled_entries"] / len(trades)
                                     if trades else None),
    }


def entry_study(directory: Path) -> dict:
    data = window_data(directory, DEV[1])
    rows = [
        summarize_replay(replay_account(**data, start=DEV[0], end=DEV[1], scale=scale), policy_id)
        for policy_id, scale in entry_candidates().items()
    ]
    baseline, *variants = rows
    for row in rows:
        row["delta_pp"] = row["net_pct"] - baseline["net_pct"]
    ranked = sorted(variants, key=rank_key)
    representatives = [
        next(row for row in ranked if row["scale"]["entry_parts"] == parts)
        for parts in (2, 3, 5, 7)
    ]
    return {
        "input_sha256": FINGERPRINTS["dev"],
        "baseline": baseline,
        "ranked_entries": ranked,
        "exit_study_entries": [
            {"policy_id": row["policy_id"], "scale": row["scale"]}
            for row in [baseline, *representatives]
        ],
    }


def read_entry_representatives(path: Path) -> dict[str, ScalePolicy]:
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != ENTRY_STUDY_SHA256:
        raise ValueError("ENTRY_STUDY_RESULT_HASH_MISMATCH")
    result = json.loads(raw)
    if result["input_sha256"] != FINGERPRINTS["dev"]:
        raise ValueError("ENTRY_STUDY_DEVELOPMENT_INPUT_MISMATCH")
    selected = {row["policy_id"]: ScalePolicy(**row["scale"])
                for row in result["exit_study_entries"]}
    if (list(policy.entry_parts for policy in selected.values()) != [1, 2, 3, 5, 7]
            or any(entry_candidates().get(key) != policy for key, policy in selected.items())):
        raise ValueError("ENTRY_STUDY_REPRESENTATIVES_MISMATCH")
    return selected


def exit_candidates(entries: dict[str, ScalePolicy]) -> dict[str, ScalePolicy]:
    candidates = {}
    for entry_id, entry in entries.items():
        policy_id = "baseline" if entry_id == "baseline" else f"{entry_id}/exit-default"
        candidates[policy_id] = entry
        for parts, gap, weights, trail in product(
            (2, 3, 5, 7), (0.75, 1.5), ("equal", "front"), (2.5, 5.0),
        ):
            policy_id = f"{entry_id}/exit-{parts}-{gap:g}-{weights}-{trail:g}"
            candidates[policy_id] = replace(entry, exit_parts=parts, exit_gap_pct=gap,
                                            exit_weights=weights, trail_pct=trail)
    return candidates


def summarize_exit(result: dict, policy_id: str) -> dict:
    row = summarize_replay(result, policy_id)
    trades = result["trades"]
    return {
        **row,
        "sell_orders_per_trade": row["sell_count"] / len(trades) if trades else None,
        "fully_scaled_exits_pct": 100 * row["fully_scaled_exits"] / len(trades) if trades else None,
        "take_fills": sum(fill["reason"] == "PARTIAL_TAKE"
                          for trade in trades for fill in trade["fills"]),
        "final_exit_reasons": [trade["fills"][-1]["reason"] for trade in trades],
    }


def exit_study(directory: Path, entry_result: Path) -> dict:
    entries = read_entry_representatives(entry_result)
    data = window_data(directory, DEV[1])
    rows = [
        summarize_exit(replay_account(**data, start=DEV[0], end=DEV[1], scale=scale), policy_id)
        for policy_id, scale in exit_candidates(entries).items()
    ]
    baseline, *variants = rows
    for row in rows:
        row["delta_pp"] = row["net_pct"] - baseline["net_pct"]
    ranked = sorted(variants, key=rank_key)
    return {
        "input_sha256": FINGERPRINTS["dev"],
        "entry_result_sha256": ENTRY_STUDY_SHA256,
        "entry_representatives": {key: asdict(policy) for key, policy in entries.items()},
        "baseline": baseline,
        "ranked_exits": ranked,
        "walk_study_candidates": [
            {"policy_id": row["policy_id"], "scale": row["scale"]}
            for row in [baseline, *ranked[:8]]
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    study = parser.add_mutually_exclusive_group()
    study.add_argument("--entries", action="store_true", help="Run the fixed 32-policy entry grid")
    study.add_argument("--exits-from", type=Path, metavar="ENTRIES_JSON",
                       help="Run the fixed exit grid for the saved entry representatives")
    args = parser.parse_args()
    if args.exits_from:
        result = exit_study(args.snapshot, args.exits_from)
    elif args.entries:
        result = entry_study(args.snapshot)
    else:
        result = replay_account(**window_data(args.snapshot, DEV[1]), start=DEV[0], end=DEV[1])
    print(json.dumps(result, indent=2, allow_nan=False))
