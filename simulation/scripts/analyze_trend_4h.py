#!/usr/bin/env python3
"""Scan pairs with Trend4hModel and write comparative table."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.trend_4h import Trend4hModel  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timeframe", default="5m", help="source TF to resample → 4h")
    ap.add_argument("--timerange", default="20260601-20260720")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--out",
        default=str(ROOT / "simulation/data/trend_4h_scan.json"),
    )
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC")

    ds = HistoricalDatastore(ROOT / "simulation/data/freqtrade")
    model = Trend4hModel()
    pairs = pairs_from_source(ROOT, args.pairs)
    if args.limit:
        pairs = pairs[: args.limit]

    rows = []
    for i, pair in enumerate(pairs, 1):
        try:
            df = ds.load(pair, args.timeframe)
        except FileNotFoundError:
            print(f"[{i}/{len(pairs)}] SKIP {pair}")
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if len(df) < 500:
            print(f"[{i}/{len(pairs)}] SKIP short {pair}")
            continue
        snap = model.snapshot(df, pair=pair)
        if snap is None:
            print(f"[{i}/{len(pairs)}] SKIP no trend {pair}")
            continue
        d = {
            "pair": snap.pair,
            "base": pair.split("/")[0],
            "asof": snap.asof,
            "close": snap.close,
            "regime": snap.regime,
            "trend_dir": snap.trend_dir,
            "trend_score": snap.trend_score,
            "trend_strength": snap.trend_strength,
            "adx": snap.adx,
            "ema_align": snap.ema_align,
            "structure": snap.structure,
            "momentum": snap.momentum,
            "di_plus": snap.di_plus,
            "di_minus": snap.di_minus,
        }
        rows.append(d)
        arrow = {1: "UP", -1: "DN", 0: "FL"}[snap.trend_dir]
        print(
            f"[{i}/{len(pairs)}] {d['base']:12} {arrow:2} {snap.regime:11} "
            f"score={snap.trend_score:+.3f} adx={snap.adx:5.1f} "
            f"ema={snap.ema_align:+.2f} mom={snap.momentum:+.2f}"
        )

    rows.sort(key=lambda r: (-abs(r["trend_score"]), -r["adx"]))
    up = [r for r in rows if r["trend_dir"] == 1]
    dn = [r for r in rows if r["trend_dir"] == -1]
    flat = [r for r in rows if r["trend_dir"] == 0]

    out = {
        "meta": {
            "model": "Trend4hModel",
            "source_tf": args.timeframe,
            "timerange": args.timerange,
            "n_pairs": len(rows),
            "components": "ema_align + adx*di + swing_structure + momentum",
        },
        "summary": {
            "trend_up": len(up),
            "trend_down": len(dn),
            "range": len(flat),
            "strongest_up": up[:20],
            "strongest_down": dn[:20],
            "strongest_abs": rows[:30],
        },
        "pairs": rows,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    csv_path = out_path.with_suffix(".csv")
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\nWrote {out_path}")
    print(f"Wrote {csv_path}")
    print(f"UP={len(up)} DOWN={len(dn)} RANGE={len(flat)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
