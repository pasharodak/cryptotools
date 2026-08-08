#!/usr/bin/env python3
"""Per-pair stats for barrier GRU filter (TP before SL) across prod200."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.trend_4h import Trend4hModel  # noqa: E402
from simulation.ml.trend_4h_rnn import _normalize, build_feature_frame  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.train_eval_trend_4h_rnn_80 import (  # noqa: E402
    EXT_COLS,
    Trend4hGRUv2,
    _attach_btc,
    predict_proba,
    split_sizes,
)
from simulation.scripts.train_eval_trend_4h_barrier80 import make_barrier_sequences  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument(
        "--model",
        default=str(ROOT / "simulation/data/models/barrier_tp15_sl5.pt"),
    )
    ap.add_argument("--conf-thr", type=float, default=0.60)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--out",
        default=str(ROOT / "simulation/data/barrier_per_pair_stats.json"),
    )
    args = ap.parse_args()

    blob = torch.load(args.model, map_location="cpu", weights_only=False)
    mean = np.asarray(blob["mean"])
    std = np.asarray(blob["std"])
    window = int(blob["window"])
    tp = float(blob["tp"])
    sl = float(blob["sl"])
    max_bars = int(blob.get("max_bars", 24))
    side_mode = str(blob.get("side_mode", "rule"))

    start_s, end_s = args.timerange.split("-")
    start, end = pd.Timestamp(start_s, tz="UTC"), pd.Timestamp(end_s, tz="UTC")
    pairs = pairs_from_source(ROOT, args.pairs)
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")

    btc = ds.load("BTC/USDT:USDT", "5m")
    btc = btc.loc[(btc.index >= start) & (btc.index < end)]
    btc_feat = build_feature_frame(btc)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = Trend4hGRUv2(n_feat=len(EXT_COLS)).to(device)
    model.load_state_dict(blob["state_dict"])
    model.eval()

    rows = []
    for i, pair in enumerate(pairs, 1):
        if args.limit and i > args.limit:
            break
        if pair.startswith("BTC/"):
            continue
        base = pair.split("/")[0]
        try:
            raw = ds.load(pair, "5m")
        except FileNotFoundError:
            print(f"[{i}] SKIP no data {base}")
            continue
        raw = raw.loc[(raw.index >= start) & (raw.index < end)]
        if len(raw) < 3000:
            print(f"[{i}] SKIP short {base}")
            continue

        h4 = Trend4hModel().build_4h_frame(raw)
        feat = _attach_btc(build_feature_frame(raw), btc_feat)
        X, y, ret, side = make_barrier_sequences(
            feat,
            h4,
            window=window,
            tp=tp,
            sl=sl,
            max_bars=max_bars,
            side_mode=side_mode,
        )
        n = len(y)
        n_tr, n_va, n_te = split_sizes(n)
        if n_tr <= 0 or n_te < 10:
            print(f"[{i}] SKIP thin {base} n={n}")
            continue

        # OOS = test slice only (same protocol as training)
        slc = slice(n_tr + n_va, n)
        Xte, yte, rte = X[slc], y[slc], ret[slc]
        Xn = _normalize(Xte, mean, std)
        proba = predict_proba(model, Xn, device)
        pred = proba.argmax(1)
        conf = proba.max(1)
        take = (pred == 1) & (conf >= args.conf_thr)

        n_all = int(len(yte))
        n_take = int(take.sum())
        base_wr = float(yte.mean()) if n_all else None
        base_exp = float(rte.mean()) if n_all else None
        if n_take >= 5:
            filt_wr = float(yte[take].mean())
            filt_exp = float(rte[take].mean())
            filt_med = float(np.median(rte[take]))
        else:
            filt_wr = filt_exp = filt_med = None

        long_share = float((side[slc] > 0).mean()) if n_all else None
        row = {
            "pair": pair,
            "base": base,
            "n_test": n_all,
            "n_take": n_take,
            "coverage_pct": round(100 * n_take / n_all, 1) if n_all else 0,
            "baseline_wr": round(100 * base_wr, 2) if base_wr is not None else None,
            "baseline_avg_ret_pct": round(100 * base_exp, 3) if base_exp is not None else None,
            "filter_wr": round(100 * filt_wr, 2) if filt_wr is not None else None,
            "filter_avg_ret_pct": round(100 * filt_exp, 3) if filt_exp is not None else None,
            "filter_med_ret_pct": round(100 * filt_med, 3) if filt_med is not None else None,
            "lift_wr_pp": (
                round(100 * (filt_wr - base_wr), 2)
                if filt_wr is not None and base_wr is not None
                else None
            ),
            "long_share_pct": round(100 * long_share, 1) if long_share is not None else None,
        }
        rows.append(row)
        wr_s = f"{row['filter_wr']:.1f}%" if row["filter_wr"] is not None else "n/a"
        print(
            f"[{i}/{len(pairs)}] {base:12} take={n_take:4}/{n_all:<4} "
            f"baseWR={row['baseline_wr']:.1f}% filtWR={wr_s} "
            f"exp={row['filter_avg_ret_pct']}"
        )

    rows.sort(key=lambda r: (-(r["filter_wr"] or -1), -(r["n_take"] or 0)))

    taken = [r for r in rows if r["filter_wr"] is not None]
    # pooled approx via weighted by n_take
    if taken:
        w_n = np.array([r["n_take"] for r in taken], dtype=float)
        w_wr = np.array([r["filter_wr"] for r in taken], dtype=float)
        w_exp = np.array([r["filter_avg_ret_pct"] for r in taken], dtype=float)
        pooled_wr = float(np.average(w_wr, weights=w_n))
        pooled_exp = float(np.average(w_exp, weights=w_n))
    else:
        pooled_wr = pooled_exp = None

    be_wr = 100 * sl / (tp + sl)  # approx breakeven winrate ignoring fees

    summary = {
        "n_pairs": len(rows),
        "n_pairs_with_filter_trades": len(taken),
        "tp": tp,
        "sl": sl,
        "conf_thr": args.conf_thr,
        "breakeven_wr_pct": round(be_wr, 2),
        "median_baseline_wr": round(float(np.median([r["baseline_wr"] for r in rows if r["baseline_wr"] is not None])), 2),
        "median_filter_wr": round(float(np.median([r["filter_wr"] for r in taken])), 2) if taken else None,
        "mean_filter_wr": round(float(np.mean([r["filter_wr"] for r in taken])), 2) if taken else None,
        "pooled_filter_wr": round(pooled_wr, 2) if pooled_wr is not None else None,
        "pooled_filter_avg_ret_pct": round(pooled_exp, 3) if pooled_exp is not None else None,
        "pct_pairs_wr_ge_80": round(100 * sum(1 for r in taken if r["filter_wr"] >= 80) / max(len(taken), 1), 1),
        "pct_pairs_wr_ge_breakeven": round(
            100 * sum(1 for r in taken if r["filter_wr"] >= be_wr) / max(len(taken), 1), 1
        ),
        "pct_pairs_positive_exp": round(
            100 * sum(1 for r in taken if (r["filter_avg_ret_pct"] or 0) > 0) / max(len(taken), 1), 1
        ),
        "total_take_trades": int(sum(r["n_take"] for r in rows)),
        "total_test_signals": int(sum(r["n_test"] for r in rows)),
    }

    out = {
        "meta": {
            "model": str(args.model),
            "timerange": args.timerange,
            "split": "per-pair test 15% (time-ordered)",
            "tp_pct": tp * 100,
            "sl_pct": sl * 100,
            "conf_thr": args.conf_thr,
            "side_mode": side_mode,
            "max_bars": max_bars,
        },
        "summary": summary,
        "best_pairs": taken[:30],
        "worst_pairs": list(reversed(taken[-30:])) if taken else [],
        "pairs": rows,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    csv_path = out_path.with_suffix(".csv")
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nWrote {out_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
