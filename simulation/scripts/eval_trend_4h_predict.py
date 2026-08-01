#!/usr/bin/env python3
"""Validate Trend4hModel predictive power via forward returns.

At each 4h bar close t (signal uses data ≤ t), measure return to t+h.
Hit = sign(forward_ret) matches trend_dir (for dir ≠ 0).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.trend_4h import Trend4hModel  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

# horizons in 4h bars
HORIZONS = {
    "4h": 1,
    "12h": 3,
    "24h": 6,
    "3d": 18,
}


def _agg(rets: list[float], hits: list[bool]) -> dict:
    if not rets:
        return {
            "n": 0,
            "hit_rate": None,
            "avg_ret_pct": None,
            "med_ret_pct": None,
            "avg_signed_pct": None,
            "sharpe_approx": None,
        }
    arr = np.asarray(rets, dtype=float)
    hit_arr = np.asarray(hits, dtype=bool) if hits else np.array([], dtype=bool)
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    return {
        "n": int(len(arr)),
        "hit_rate": round(100.0 * float(hit_arr.mean()), 1) if len(hit_arr) else None,
        "avg_ret_pct": round(mu * 100, 3),
        "med_ret_pct": round(float(np.median(arr)) * 100, 3),
        "avg_signed_pct": round(mu * 100, 3),
        "sharpe_approx": round(mu / (sd + 1e-12) * np.sqrt(len(arr)), 2) if sd else None,
    }


def eval_pair(h4: pd.DataFrame) -> dict[str, dict]:
    """Return nested stats: regime_bucket -> horizon -> metrics."""
    close = h4["close"].astype(float).to_numpy()
    direction = h4["trend_4h_dir"].to_numpy(dtype=int)
    strength = h4["trend_4h_strength"].to_numpy(dtype=float)
    regime = h4["trend_4h_regime"].astype(str).to_numpy()
    score = h4["trend_4h_score"].to_numpy(dtype=float)

    # regime change entries (first bar of new trend)
    prev = np.roll(regime, 1)
    prev[0] = "range"
    entry_up = (regime == "trend_up") & (prev != "trend_up")
    entry_dn = (regime == "trend_down") & (prev != "trend_down")

    buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: {"rets": [], "hits": []}))

    n = len(close)
    max_h = max(HORIZONS.values())
    for i in range(n - max_h):
        if not np.isfinite(score[i]):
            continue
        d = int(direction[i])
        for name, h in HORIZONS.items():
            fwd = (close[i + h] / close[i]) - 1.0
            # signed return if we trade with the trend
            if d == 0:
                signed = 0.0  # flat: no directional trade
                hit = abs(fwd) < 0.01  # "correct" range ≈ small move (loose)
                buckets["range"][name]["rets"].append(fwd)
                buckets["range"][name]["hits"].append(bool(hit))
            else:
                signed = d * fwd
                hit = (fwd > 0 and d > 0) or (fwd < 0 and d < 0)
                key = "trend_up" if d > 0 else "trend_down"
                buckets[key][name]["rets"].append(signed)
                buckets[key][name]["hits"].append(bool(hit))
                buckets["any_trend"][name]["rets"].append(signed)
                buckets["any_trend"][name]["hits"].append(bool(hit))
                if strength[i] >= 0.5:
                    buckets["strong"][name]["rets"].append(signed)
                    buckets["strong"][name]["hits"].append(bool(hit))
                elif strength[i] >= 0.25:
                    buckets["medium"][name]["rets"].append(signed)
                    buckets["medium"][name]["hits"].append(bool(hit))
                else:
                    buckets["weak"][name]["rets"].append(signed)
                    buckets["weak"][name]["hits"].append(bool(hit))

            if entry_up[i]:
                buckets["entry_up"][name]["rets"].append(fwd)
                buckets["entry_up"][name]["hits"].append(bool(fwd > 0))
            if entry_dn[i]:
                buckets["entry_dn"][name]["rets"].append(-fwd)
                buckets["entry_dn"][name]["hits"].append(bool(fwd < 0))

        # baseline: always long
        for name, h in HORIZONS.items():
            fwd = (close[i + h] / close[i]) - 1.0
            buckets["baseline_long"][name]["rets"].append(fwd)
            buckets["baseline_long"][name]["hits"].append(bool(fwd > 0))

    out: dict[str, dict] = {}
    for buck, by_h in buckets.items():
        out[buck] = {h: _agg(v["rets"], v["hits"]) for h, v in by_h.items()}
    return out


def merge_stats(acc: dict, part: dict) -> None:
    """Accumulate raw lists into acc structure for global re-agg — simpler: weighted merge of means."""
    for buck, by_h in part.items():
        if buck not in acc:
            acc[buck] = {h: {"rets": [], "hits": []} for h in HORIZONS}
        for h, m in by_h.items():
            # we only have aggregates — re-run per pair storing raw is better
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--out",
        default=str(ROOT / "simulation/data/trend_4h_predict_eval.json"),
    )
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC")

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    model = Trend4hModel()
    pairs = pairs_from_source(ROOT, args.pairs)
    if args.limit:
        pairs = pairs[: args.limit]

    # accumulate raw
    raw: dict[str, dict[str, dict[str, list]]] = defaultdict(
        lambda: {h: {"rets": [], "hits": []} for h in HORIZONS}
    )
    per_pair = []

    for i, pair in enumerate(pairs, 1):
        try:
            df = ds.load(pair, args.timeframe)
        except FileNotFoundError:
            print(f"[{i}/{len(pairs)}] SKIP {pair}")
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if len(df) < 2000:
            print(f"[{i}/{len(pairs)}] SKIP short {pair}")
            continue
        h4 = model.build_4h_frame(df)
        if len(h4.dropna(subset=["trend_4h_score"])) < 80:
            print(f"[{i}/{len(pairs)}] SKIP thin {pair}")
            continue

        # inline eval collecting raw
        close = h4["close"].astype(float).to_numpy()
        direction = h4["trend_4h_dir"].to_numpy(dtype=int)
        strength = h4["trend_4h_strength"].to_numpy(dtype=float)
        regime = h4["trend_4h_regime"].astype(str).to_numpy()
        score = h4["trend_4h_score"].to_numpy(dtype=float)
        prev = np.roll(regime, 1)
        prev[0] = "range"
        entry_up = (regime == "trend_up") & (prev != "trend_up")
        entry_dn = (regime == "trend_down") & (prev != "trend_down")
        max_h = max(HORIZONS.values())
        pair_hits = {h: [] for h in HORIZONS}
        pair_rets = {h: [] for h in HORIZONS}

        for j in range(len(close) - max_h):
            if not np.isfinite(score[j]):
                continue
            d = int(direction[j])
            for name, h in HORIZONS.items():
                fwd = (close[j + h] / close[j]) - 1.0
                raw["baseline_long"][name]["rets"].append(fwd)
                raw["baseline_long"][name]["hits"].append(bool(fwd > 0))

                if d == 0:
                    raw["range"][name]["rets"].append(fwd)
                    raw["range"][name]["hits"].append(bool(abs(fwd) < 0.01))
                    continue

                signed = d * fwd
                hit = (fwd > 0 and d > 0) or (fwd < 0 and d < 0)
                key = "trend_up" if d > 0 else "trend_down"
                raw[key][name]["rets"].append(signed)
                raw[key][name]["hits"].append(bool(hit))
                raw["any_trend"][name]["rets"].append(signed)
                raw["any_trend"][name]["hits"].append(bool(hit))
                pair_hits[name].append(bool(hit))
                pair_rets[name].append(signed)

                if strength[j] >= 0.5:
                    raw["strong"][name]["rets"].append(signed)
                    raw["strong"][name]["hits"].append(bool(hit))
                elif strength[j] >= 0.25:
                    raw["medium"][name]["rets"].append(signed)
                    raw["medium"][name]["hits"].append(bool(hit))
                else:
                    raw["weak"][name]["rets"].append(signed)
                    raw["weak"][name]["hits"].append(bool(hit))

                if entry_up[j]:
                    raw["entry_up"][name]["rets"].append(fwd)
                    raw["entry_up"][name]["hits"].append(bool(fwd > 0))
                if entry_dn[j]:
                    raw["entry_dn"][name]["rets"].append(-fwd)
                    raw["entry_dn"][name]["hits"].append(bool(fwd < 0))

        if pair_hits["24h"]:
            per_pair.append(
                {
                    "pair": pair,
                    "base": pair.split("/")[0],
                    "n": len(pair_hits["24h"]),
                    "hit_24h": round(100 * float(np.mean(pair_hits["24h"])), 1),
                    "avg_signed_24h_pct": round(100 * float(np.mean(pair_rets["24h"])), 3),
                    "hit_3d": round(100 * float(np.mean(pair_hits["3d"])), 1),
                    "avg_signed_3d_pct": round(100 * float(np.mean(pair_rets["3d"])), 3),
                }
            )
            print(
                f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} "
                f"hit24h={per_pair[-1]['hit_24h']:5.1f}% "
                f"R24h={per_pair[-1]['avg_signed_24h_pct']:+.3f}% "
                f"hit3d={per_pair[-1]['hit_3d']:5.1f}%"
            )

    global_stats = {
        buck: {h: _agg(v["rets"], v["hits"]) for h, v in by_h.items()}
        for buck, by_h in raw.items()
    }

    per_pair.sort(key=lambda r: -r["hit_24h"])
    out = {
        "meta": {
            "model": "Trend4hModel",
            "timerange": args.timerange,
            "source_tf": args.timeframe,
            "horizons": HORIZONS,
            "n_pairs": len(per_pair),
            "note": (
                "signed return = trend_dir * forward_ret; "
                "hit = price moved in predicted direction"
            ),
        },
        "global": global_stats,
        "best_pairs_24h": per_pair[:25],
        "worst_pairs_24h": list(reversed(per_pair[-25:])),
        "pairs": per_pair,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== GLOBAL (trade with trend) ===")
    for buck in ("any_trend", "strong", "medium", "weak", "entry_up", "entry_dn", "baseline_long"):
        if buck not in global_stats:
            continue
        print(f"\n{buck}:")
        for h in HORIZONS:
            m = global_stats[buck][h]
            if not m["n"]:
                continue
            print(
                f"  {h:4} n={m['n']:6} hit={m['hit_rate']:5.1f}% "
                f"avg={m['avg_ret_pct']:+.3f}% med={m['med_ret_pct']:+.3f}%"
            )
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
