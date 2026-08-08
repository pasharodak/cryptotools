#!/usr/bin/env python3
"""Conditional reaction delay: after a strong BTC move, when does the alt follow?"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402


def returns(close: pd.Series) -> pd.Series:
    return np.log(close.astype(float)).diff().replace([np.inf, -np.inf], np.nan)


def reaction_delays(
    btc: np.ndarray,
    alt: np.ndarray,
    *,
    thr: float,
    horizon: int,
) -> dict:
    """For |btc|>=thr, find first k in 0..horizon where sign(alt[t+k])==sign(btc[t])."""
    delays = []
    same0 = 0
    events = 0
    for t in range(len(btc) - horizon - 1):
        br = btc[t]
        if abs(br) < thr:
            continue
        events += 1
        sign = 1 if br > 0 else -1
        found = None
        for k in range(0, horizon + 1):
            if alt[t + k] * sign > 0:
                found = k
                break
        if found is None:
            continue
        delays.append(found)
        if found == 0:
            same0 += 1
    if not delays:
        return {
            "events": events,
            "n_react": 0,
            "pct_same_minute": None,
            "median_delay_min": None,
            "mean_delay_min": None,
            "p90_delay_min": None,
            "hist": {},
        }
    arr = np.asarray(delays, dtype=float)
    hist = {str(i): int((arr == i).sum()) for i in range(horizon + 1)}
    return {
        "events": events,
        "n_react": int(len(arr)),
        "pct_same_minute": round(100.0 * same0 / len(arr), 1),
        "median_delay_min": float(np.median(arr)),
        "mean_delay_min": round(float(arr.mean()), 2),
        "p90_delay_min": float(np.percentile(arr, 90)),
        "hist": hist,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timerange", default="20260620-20260716")
    ap.add_argument("--thr-bps", type=float, default=8.0, help="BTC move threshold in bps")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument(
        "--out",
        default=str(ROOT / "simulation/data/btc_reaction_delay_1m.json"),
    )
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC")
    thr = args.thr_bps / 10000.0

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    btc = ds.load("BTC/USDT:USDT", "1m")
    btc = btc.loc[(btc.index >= start) & (btc.index < end)]
    btc_r = returns(btc["close"]).dropna()

    pairs = [p for p in pairs_from_source(ROOT, args.pairs) if not p.startswith("BTC/")]
    rows = []
    for i, pair in enumerate(pairs, 1):
        try:
            df = ds.load(pair, "1m")
        except FileNotFoundError:
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        alt_r = returns(df["close"])
        joined = pd.concat({"btc": btc_r, "alt": alt_r}, axis=1).dropna()
        if len(joined) < 1000:
            continue
        b = joined["btc"].to_numpy(dtype=np.float64)
        a = joined["alt"].to_numpy(dtype=np.float64)
        corr0 = float(np.corrcoef(b, a)[0, 1])
        stats = reaction_delays(b, a, thr=thr, horizon=args.horizon)
        row = {
            "pair": pair,
            "base": pair.split("/")[0],
            "corr_0": round(corr0, 4),
            **stats,
        }
        rows.append(row)
        md = stats["median_delay_min"]
        print(
            f"[{i}/{len(pairs)}] {row['base']:12} corr={corr0:+.3f} "
            f"same_min={stats['pct_same_minute']}% med={md} "
            f"p90={stats['p90_delay_min']} events={stats['events']}"
        )

    # Only meaningful followers for summary
    good = [r for r in rows if (r["corr_0"] or 0) >= 0.25 and r["n_react"] >= 50]
    good_sorted = sorted(good, key=lambda r: (r["median_delay_min"] or 99, -(r["corr_0"] or 0)))
    out = {
        "meta": {
            "timeframe": "1m",
            "timerange": args.timerange,
            "btc_move_threshold_bps": args.thr_bps,
            "horizon_min": args.horizon,
            "n_pairs": len(rows),
            "note": (
                "After |BTC 1m return| >= threshold, delay = first minute "
                "where alt moves in the same direction (0 = same minute)."
            ),
        },
        "summary": {
            "median_of_medians_min": float(
                np.median([r["median_delay_min"] for r in good if r["median_delay_min"] is not None])
            )
            if good
            else None,
            "mean_pct_same_minute": round(
                float(np.mean([r["pct_same_minute"] for r in good if r["pct_same_minute"] is not None])),
                1,
            )
            if good
            else None,
            "fastest": good_sorted[:25],
            "slowest": list(reversed(good_sorted[-25:])),
        },
        "pairs": sorted(rows, key=lambda r: (r["median_delay_min"] is None, r["median_delay_min"] or 99, -abs(r["corr_0"] or 0))),
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}")
    print(
        f"Among corr>=0.25: mean same-minute {out['summary']['mean_pct_same_minute']}% · "
        f"median-of-medians {out['summary']['median_of_medians_min']} min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
