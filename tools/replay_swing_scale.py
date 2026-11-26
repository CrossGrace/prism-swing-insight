"""Run the preregistered OCI account baseline against immutable research inputs."""

from __future__ import annotations

import argparse
from hashlib import sha256
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swing_insight.scale_replay import replay_account

DEV = ("2023-10-04", "2024-12-30")
WALK = (("2025-01-02", "2025-04-30"), ("2025-05-02", "2025-08-29"))
FINGERPRINTS = {
    "dev": "7d1848066b8d5182a56ec5dee7f153c3ee12011109e8635696f7b2aefded1243",
    "walk": "2e15afa2a1562e82f8dfffb6065c15539ce6a6ebe091eea25bb6ba21304de479",
}
PREFIX = "PRISM_OCI_BLIND_SCALE_20261127_"


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay_account(**window_data(args.snapshot, DEV[1]), start=DEV[0], end=DEV[1]),
                     indent=2, allow_nan=False))
