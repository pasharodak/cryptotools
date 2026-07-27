"""Shared helpers for period comparisons (pairs, months, timerange)."""
from __future__ import annotations

import json
from pathlib import Path

from simulation.exchange_sim.datastore import HistoricalDatastore

DEFAULT_TIMERANGE = "20250101-20260704"
PROD_PAIRS_PATH = "simulation/config/prod_pairs_200.json"
GRID_PRIORITY_PATH = "simulation/config/grid_priority_pairs.json"


def period_months(start_year: int = 2025, start_month: int = 1, end_year: int = 2026, end_month: int = 7) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def pairs_from_source(root: Path, source: str, *, min_start: str | None = None) -> list[str]:
    datadir = root / "simulation/data/freqtrade"
    ds = HistoricalDatastore(datadir)

    if source == "pool":
        path = root / "simulation/config/player_pair_pool.json"
        pairs = json.loads(path.read_text(encoding="utf-8"))["pairs"]
    elif source == "extension":
        path = root / "simulation/config/extension_pairs.json"
        pairs = json.loads(path.read_text(encoding="utf-8"))["pairs"]
    elif source == "all":
        pairs = set(ds.list_pairs("5m"))
        export = root / "simulation/data/live_trades_export.json"
        if export.is_file():
            pairs.update(json.loads(export.read_text(encoding="utf-8")).get("all_pairs") or [])
        pairs = sorted(pairs)
    elif source in ("prod200", "prod"):
        path = root / PROD_PAIRS_PATH
        if not path.is_file():
            raise FileNotFoundError(f"prod pairs not found — run fetch_prod_pairs.py first: {path}")
        pairs = list(json.loads(path.read_text(encoding="utf-8")).get("pairs") or [])
    elif source == "grid_priority":
        path = root / GRID_PRIORITY_PATH
        pairs = list(json.loads(path.read_text(encoding="utf-8")).get("pairs") or [])
    else:
        raise ValueError(f"unknown pairs source: {source}")

    if not min_start:
        return pairs

    import pandas as pd

    want = pd.Timestamp(min_start, tz="UTC")
    out: list[str] = []
    for p in pairs:
        try:
            df = ds.load(p, "5m")
            if not df.empty and df.index[0] <= want:
                out.append(p)
        except FileNotFoundError:
            continue
    return out


def update_manifest_timerange(root: Path, timerange: str) -> None:
    path = root / "simulation/config/manifest.json"
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg["timerange"] = timerange
    cfg["player_timerange"] = timerange
    path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
