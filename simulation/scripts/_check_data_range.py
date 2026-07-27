#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
datadir = ROOT / "simulation/data/freqtrade"
pairs = json.loads((ROOT / "simulation/config/manifest.json").read_text(encoding="utf-8"))["player_pairs"]

for sub in ["bybit", "futures"]:
    print(f"\n=== {sub}/ ===")
    base = datadir / sub
    if not base.is_dir():
        print("  (missing)")
        continue
    for p in pairs:
        sym = p.replace("/", "_").replace(":", "_")
        for tf in ["5m", "1s"]:
            fp = base / f"{sym}-{tf}-futures.feather"
            if not fp.is_file():
                print(f"  {p} {tf}: MISSING")
                continue
            df = pd.read_feather(fp)
            t0 = pd.to_datetime(df.date.iloc[0], unit="ms")
            t1 = pd.to_datetime(df.date.iloc[-1], unit="ms")
            print(f"  {p} {tf}: {t0.date()} .. {t1.date()} ({len(df)})")
