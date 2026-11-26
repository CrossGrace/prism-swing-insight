"""Freeze local, read-only OCI research price snapshots into disjoint archives."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import gzip
import json
from pathlib import Path
from xml.etree import ElementTree

SYMBOL = "010060"
WARMUP = "2023-06-01"
DEV_END = "2024-12-30"
WALK_END = "2025-08-29"
HOLDOUT_END = "2026-06-16"
FIELDS = ("Open", "High", "Low", "Close", "Volume")


def _day(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _append(rows: dict, raw_day: str, values: tuple) -> None:
    day = _day(raw_day)
    opening, high, low, close, volume = values
    if (day in rows or not 0 < low <= min(opening, close) <= max(opening, close) <= high
            or not volume > 0):
        raise ValueError(f"INVALID_OR_DUPLICATE_DAILY_BAR:{day}")
    rows[day] = dict(zip(FIELDS, values))


def xml_prices(path: Path, symbol: str) -> dict:
    root = ElementTree.fromstring(path.read_bytes().decode("euc-kr"))
    chart = root.find(".//chartdata")
    if chart is None or chart.get("symbol") != symbol or chart.get("timeframe") != "day":
        raise ValueError("NAVER_XML_SCHEMA_CHANGED")
    rows: dict = {}
    for item in chart.findall("item"):
        day, *values = item.attrib["data"].split("|")
        _append(rows, day, tuple(map(float, values)))
    if len(rows) != int(chart.attrib["count"]):
        raise ValueError("NAVER_XML_COUNT_MISMATCH")
    return rows


def fsc_prices(path: Path, symbol: str) -> tuple[dict, list]:
    raw = json.loads(path.read_bytes())
    if raw.get("code") != symbol or raw["columns"] != ["date", "close", "volume", "open", "high", "low"]:
        raise ValueError("FSC_SCHEMA_CHANGED")
    rows: dict = {}
    for day, close, volume, opening, high, low in raw["rows"]:
        if symbol == "010120" and day in {"20260408", "20260409", "20260410"} and not volume:
            continue  # Disclosed split halt in the auxiliary instrument.
        if WARMUP <= _day(day) <= HOLDOUT_END:
            _append(rows, day, (opening, high, low, close, volume))
    return rows, raw["breaks"]


def _pack(path: Path, stock: dict, index: dict) -> str:
    payload = json.dumps({"stock": stock, "index": index}, sort_keys=True, separators=(",", ":"))
    path.write_bytes(gzip.compress(payload.encode(), mtime=0))
    return sha256(path.read_bytes()).hexdigest()


def freeze(source: Path, target: Path) -> dict:
    stock, actions = fsc_prices(source / "010060-fsc.json", SYMBOL)
    index = xml_prices(source / "KOSPI.xml", "KOSPI")
    naver_stock = xml_prices(source / "010060.xml", SYMBOL)
    if any(WARMUP <= _day(action["date"]) <= HOLDOUT_END for action in actions):
        raise ValueError("OCI_CORPORATE_ACTION_DURING_RESEARCH")
    index_days = {day for day in index if WARMUP <= day <= HOLDOUT_END}
    stock_days = {day for day in stock if WARMUP <= day <= HOLDOUT_END}
    if index_days != stock_days or any(stock[day] != naver_stock.get(day) for day in stock_days):
        raise ValueError("OCI_DAILY_CROSS_SOURCE_OR_CALENDAR_MISMATCH")
    ls, ls_actions = fsc_prices(source / "010120-fsc.json", "010120")
    alternate_index = json.loads((source / "KOSPI-dashboard.json").read_bytes())
    if alternate_index.get("code") != "KOSPI" or not alternate_index.get("hasVolume"):
        raise ValueError("NAVER_INDEX_JSON_SCHEMA_CHANGED")
    index_matches = 0
    for row in alternate_index["priceInfos"]:
        values = dict(zip(FIELDS, (row["openPrice"], row["highPrice"], row["lowPrice"],
                                    row["closePrice"], row["accumulatedTradingVolume"])))
        if index[_day(row["localDate"])] != values:
            raise ValueError("NAVER_KOSPI_CROSS_ENDPOINT_MISMATCH")
        index_matches += 1

    target.mkdir(parents=True, exist_ok=True)
    report: dict = {"index_cross_check_sessions": index_matches, "oci_actions": actions,
                    "ls_actions": ls_actions, "ls_missing_sessions": sorted(stock_days - ls.keys())}
    begin = WARMUP
    for name, end in (("dev", DEV_END), ("walk", WALK_END), ("holdout", HOLDOUT_END)):
        selected = sorted(day for day in stock_days if begin <= day <= end)
        path = target / f"PRISM_OCI_BLIND_SCALE_20261127_{name}.json.gz"
        digest = _pack(path, {day: stock[day] for day in selected},
                       {day: index[day] for day in selected})
        report[name] = {"first": selected[0], "last": selected[-1], "sessions": len(selected),
                        "sha256": digest, "file": path.name}
        begin = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    report["raw_sha256"] = {path.name: sha256(path.read_bytes()).hexdigest()
                            for path in sorted(source.iterdir()) if path.is_file()}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.raw, args.output), indent=2))
