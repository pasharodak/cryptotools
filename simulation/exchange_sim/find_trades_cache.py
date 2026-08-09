"""Disk cache for «Отображение сделок» — reuse backtested trades across requests."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


CACHE_DIR = Path("simulation/data/cache/find_trades")


def _safe_pair(pair: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", pair)


def cache_path(root: Path, scenario_id: str, pair: str) -> Path:
    return root / CACHE_DIR / f"{scenario_id}__{_safe_pair(pair)}.json"


def _trade_key(tr: dict[str, Any]) -> tuple:
    return (
        tr.get("pair") or "",
        int(tr.get("open_ms") or 0),
        int(tr.get("close_ms") or 0),
        round(float(tr.get("open_rate") or 0), 8),
    )


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    if not ranges:
        return []
    ordered = sorted(([int(a), int(b)] for a, b in ranges if a is not None and b is not None), key=lambda x: x[0])
    out: list[list[int]] = []
    for start, end in ordered:
        if end < start:
            start, end = end, start
        if not out or start > out[-1][1] + 1:
            out.append([start, end])
        else:
            out[-1][1] = max(out[-1][1], end)
    return out


def missing_ranges(start_ms: int, end_ms: int, covered: list[list[int]]) -> list[list[int]]:
    """Parts of [start_ms, end_ms] not yet covered by cached ranges."""
    if end_ms <= start_ms:
        return []
    gaps: list[list[int]] = []
    cursor = start_ms
    for a, b in _merge_ranges(covered):
        if b < cursor:
            continue
        if a > end_ms:
            break
        if a > cursor:
            gaps.append([cursor, min(a, end_ms)])
        cursor = max(cursor, b)
        if cursor >= end_ms:
            break
    if cursor < end_ms:
        gaps.append([cursor, end_ms])
    return [[a, b] for a, b in gaps if b > a]


def load_entry(root: Path, scenario_id: str, pair: str) -> dict[str, Any] | None:
    path = cache_path(root, scenario_id, pair)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("ranges", [])
    data.setdefault("trades", [])
    data["scenario_id"] = scenario_id
    data["pair"] = pair
    return data


def save_entry(root: Path, scenario_id: str, pair: str, entry: dict[str, Any]) -> Path:
    path = cache_path(root, scenario_id, pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenario_id": scenario_id,
        "pair": pair,
        "ranges": _merge_ranges(list(entry.get("ranges") or [])),
        "trades": entry.get("trades") or [],
        "updated_ms": entry.get("updated_ms"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def merge_trades(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for tr in existing:
        by_key[_trade_key(tr)] = tr
    for tr in new_rows:
        by_key[_trade_key(tr)] = tr
    rows = list(by_key.values())
    rows.sort(key=lambda t: int(t.get("open_ms") or 0))
    return rows


def trades_in_range(trades: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    out = []
    for tr in trades:
        open_ms = int(tr.get("open_ms") or 0)
        if start_ms <= open_ms <= end_ms:
            out.append(tr)
    return out


def filter_trades_by_ml(trades: list[dict], *, ml_enabled: bool) -> list[dict]:
    """When ML gate is on, drop trades the model tagged as loss (if annotated)."""
    if not ml_enabled:
        return list(trades)
    kept: list[dict] = []
    for tr in trades:
        ml = tr.get("ml")
        if not isinstance(ml, dict):
            # No annotation — keep (gate was off / unavailable at cache time)
            kept.append(tr)
            continue
        predicted = str(ml.get("predicted") or "").lower()
        if predicted == "loss":
            continue
        kept.append(tr)
    return kept


def upsert_range(
    root: Path,
    scenario_id: str,
    pair: str,
    start_ms: int,
    end_ms: int,
    new_trades: list[dict],
) -> dict[str, Any]:
    entry = load_entry(root, scenario_id, pair) or {
        "scenario_id": scenario_id,
        "pair": pair,
        "ranges": [],
        "trades": [],
    }
    entry["ranges"] = _merge_ranges(list(entry.get("ranges") or []) + [[start_ms, end_ms]])
    entry["trades"] = merge_trades(list(entry.get("trades") or []), new_trades)
    from time import time

    entry["updated_ms"] = int(time() * 1000)
    save_entry(root, scenario_id, pair, entry)
    return entry
