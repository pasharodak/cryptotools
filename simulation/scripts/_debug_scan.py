#!/usr/bin/env python3
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.scan_ranging_pairs import analyze_ranging  # noqa: E402
from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.exchange_sim.scan_replay import grid_scan_at, load_scan_configs, resample_ohlcv, slice_at  # noqa: E402

pairs = json.loads((ROOT / "simulation/config/manifest.json").read_text())["player_pairs"]
ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
cfg, _, _ = load_scan_configs(ROOT)
bl = set()

for ts_s in ["2026-06-20 12:00", "2026-06-21 06:00", "2026-06-22 12:00", "2026-06-23 18:00"]:
    ts = int(datetime.fromisoformat(ts_s).replace(tzinfo=UTC).timestamp() * 1000)
    hits = grid_scan_at(ds, pairs, ts, cfg, bl)
    print(ts_s, "hits", [h["pair"].split("/")[0] for h in hits])
    for p in ["HEI/USDT:USDT", "NEAR/USDT:USDT", "SOL/USDT:USDT"]:
        df5 = slice_at(ds.load(p, "5m"), ts, 96)
        df_htf = resample_ohlcv(df5, "15min")
        m = analyze_ranging(df5, cfg, df_htf=df_htf if len(df_htf) > 20 else None)
        if m:
            print(" ", p.split("/")[0], f"ratio={m['ranging_ratio']:.2f} adx={m['adx']:.1f} bb={m['bb_width']:.3f} score={m['score']:.2f}")
        else:
            print(" ", p.split("/")[0], "FAIL")
