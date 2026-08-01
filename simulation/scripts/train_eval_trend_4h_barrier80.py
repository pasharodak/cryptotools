#!/usr/bin/env python3
"""Asymmetric barrier task: did price hit TP before SL within horizon?

This is NOT pure direction accuracy. With TP < SL (in distance), winrate can be
high while expectancy may still be weak — we report both.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.trend_4h_rnn import (  # noqa: E402
    FEATURE_COLS,
    Trend4hGRU,
    _normalize,
    build_feature_frame,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.train_eval_trend_4h_rnn_80 import (  # noqa: E402
    EXT_COLS,
    Trend4hGRUv2,
    _attach_btc,
    predict_proba,
    split_sizes,
)


def barrier_outcome(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    i: int,
    *,
    side: int,
    tp: float,
    sl: float,
    max_bars: int,
) -> tuple[int, float]:
    """Return (label, realized_ret).

    label: 1 = TP before SL, 0 = SL before TP (or time exit without TP).
    side: +1 long, -1 short. Entry at close[i].
    """
    entry = float(close[i])
    if side > 0:
        tp_px = entry * (1 + tp)
        sl_px = entry * (1 - sl)
    else:
        tp_px = entry * (1 - tp)
        sl_px = entry * (1 + sl)

    end = min(len(close) - 1, i + max_bars)
    for j in range(i + 1, end + 1):
        hi, lo = float(high[j]), float(low[j])
        if side > 0:
            hit_sl = lo <= sl_px
            hit_tp = hi >= tp_px
        else:
            hit_sl = hi >= sl_px
            hit_tp = lo <= tp_px
        if hit_tp and hit_sl:
            # conservative: same bar both → count as SL
            ret = -sl if side > 0 else -sl
            # for short, SL loss is also -sl in return space approx
            return 0, -sl
        if hit_tp:
            return 1, tp
        if hit_sl:
            return 0, -sl
    # time exit at last close
    exit_px = float(close[end])
    ret = side * (exit_px / entry - 1.0)
    return (1 if ret > 0 else 0), ret


def make_barrier_sequences(
    feat: pd.DataFrame,
    ohlc_4h: pd.DataFrame,
    *,
    window: int,
    tp: float,
    sl: float,
    max_bars: int,
    side_mode: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build sequences. side_mode: 'long'|'short'|'rule' (sign of rule_score)."""
    need = EXT_COLS + ["close", "rule_score"]
    df = feat.dropna(subset=[c for c in need if c in feat.columns])
    if any(c not in df.columns for c in EXT_COLS) or len(df) < window + max_bars + 5:
        z = (0, window, len(EXT_COLS))
        return (
            np.zeros(z, np.float32),
            np.zeros((0,), np.int64),
            np.zeros((0,), np.float32),
            np.zeros((0,), np.int64),
        )

    # align OHLC to feat index
    o = ohlc_4h.reindex(df.index)
    high = o["high"].astype(float).to_numpy()
    low = o["low"].astype(float).to_numpy()
    close = df["close"].to_numpy(dtype=np.float64)
    rule = df["rule_score"].to_numpy(dtype=np.float64)
    x_mat = df[EXT_COLS].to_numpy(dtype=np.float64)

    xs, ys, rets, sides = [], [], [], []
    for i in range(window - 1, len(df) - max_bars):
        if side_mode == "long":
            side = 1
        elif side_mode == "short":
            side = -1
        else:
            if rule[i] >= 0.18:
                side = 1
            elif rule[i] <= -0.18:
                side = -1
            else:
                continue
        lab, ret = barrier_outcome(
            high, low, close, i, side=side, tp=tp, sl=sl, max_bars=max_bars
        )
        xs.append(x_mat[i - window + 1 : i + 1])
        ys.append(lab)
        rets.append(ret)
        sides.append(side)
    return (
        np.asarray(xs, np.float32),
        np.asarray(ys, np.int64),
        np.asarray(rets, np.float32),
        np.asarray(sides, np.int64),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument("--window", type=int, default=24)
    ap.add_argument("--tp", type=float, default=0.01, help="take profit fraction")
    ap.add_argument("--sl", type=float, default=0.03, help="stop loss fraction")
    ap.add_argument("--max-bars", type=int, default=18, help="4h bars horizon (~3d if 18)")
    ap.add_argument("--side-mode", choices=("long", "short", "rule"), default="rule")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target-hit", type=float, default=80.0)
    ap.add_argument("--out", default=str(ROOT / "simulation/data/trend_4h_barrier80.json"))
    ap.add_argument("--model-out", default=str(ROOT / "simulation/data/models/trend_4h_barrier_gru.pt"))
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start, end = pd.Timestamp(start_s, tz="UTC"), pd.Timestamp(end_s, tz="UTC")
    pairs = pairs_from_source(ROOT, args.pairs)
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")

    from simulation.ml.trend_4h import Trend4hModel, resample_ohlcv

    btc_df = ds.load("BTC/USDT:USDT", "5m")
    btc_df = btc_df.loc[(btc_df.index >= start) & (btc_df.index < end)]
    btc_feat = build_feature_frame(btc_df)

    pools = {k: {"X": [], "y": [], "ret": [], "side": []} for k in ("train", "val", "test")}
    base_hits = []

    for i, pair in enumerate(pairs, 1):
        if args.limit and i > args.limit:
            break
        if pair.startswith("BTC/"):
            continue
        try:
            raw = ds.load(pair, "5m")
        except FileNotFoundError:
            continue
        raw = raw.loc[(raw.index >= start) & (raw.index < end)]
        if len(raw) < 3000:
            continue
        h4 = Trend4hModel().build_4h_frame(raw)
        feat = _attach_btc(build_feature_frame(raw), btc_feat)
        X, y, ret, side = make_barrier_sequences(
            feat,
            h4,
            window=args.window,
            tp=args.tp,
            sl=args.sl,
            max_bars=args.max_bars,
            side_mode=args.side_mode,
        )
        n = len(y)
        n_tr, n_va, n_te = split_sizes(n)
        if n_tr <= 0:
            continue
        if n:
            base_hits.append(float(y.mean()))
        sl = {
            "train": slice(0, n_tr),
            "val": slice(n_tr, n_tr + n_va),
            "test": slice(n_tr + n_va, n),
        }
        for name, s in sl.items():
            pools[name]["X"].append(X[s])
            pools[name]["y"].append(y[s])
            pools[name]["ret"].append(ret[s])
            pools[name]["side"].append(side[s])
        print(f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} n={n} winrate={100*y.mean():.1f}%")

    for name in pools:
        for k in ("X", "y", "ret", "side"):
            pools[name][k] = np.concatenate(pools[name][k])
        print(name, pools[name]["X"].shape, "WR", round(100 * pools[name]["y"].mean(), 2))

    # Baseline: always take trade (no ML) winrate on test
    yte = pools["test"]["y"]
    rte = pools["test"]["ret"]
    baseline_wr = float(yte.mean())
    baseline_exp = float(rte.mean())

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xtr, ytr = pools["train"]["X"], pools["train"]["y"]
    Xva, yva = pools["val"]["X"], pools["val"]["y"]
    Xte = pools["test"]["X"]

    mean = Xtr.reshape(-1, Xtr.shape[-1]).mean(0)
    std = Xtr.reshape(-1, Xtr.shape[-1]).std(0)
    Xtr_n, Xva_n, Xte_n = _normalize(Xtr, mean, std), _normalize(Xva, mean, std), _normalize(Xte, mean, std)

    counts = np.bincount(ytr, minlength=2).astype(np.float64)
    w = counts.sum() / (counts + 1e-6)
    w = torch.tensor(w / w.mean(), dtype=torch.float32, device=device)

    torch.manual_seed(42)
    model = Trend4hGRUv2(n_feat=len(EXT_COLS)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-4)
    loss_fn = nn.CrossEntropyLoss(weight=w)
    tr_loader = DataLoader(
        TensorDataset(torch.from_numpy(Xtr_n), torch.from_numpy(ytr)),
        batch_size=args.batch,
        shuffle=True,
    )

    best_state, best_va = None, -1.0
    for ep in range(args.epochs):
        model.train()
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss_fn(model(xb), yb).backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        with torch.no_grad():
            p = predict_proba(model, Xva_n, device).argmax(1)
            va_acc = float((p == yva).mean())
        if va_acc > best_va:
            best_va = va_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(f"epoch {ep+1}/{args.epochs} val_acc={va_acc:.3f} best={best_va:.3f}")

    if best_state:
        model.load_state_dict(best_state)
    model.to(device)

    proba = predict_proba(model, Xte_n, device)
    pred = proba.argmax(1)
    conf = proba.max(1)
    # Class 1 = predict TP wins. We only TAKE trades when model predicts win (pred==1)
    # Precision of "take trade" = among pred==1, fraction that actually won
    # Also sweep confidence

    curve = []
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95]:
        m = (pred == 1) & (conf >= thr)
        n = int(m.sum())
        if n < 30:
            curve.append({"thr": thr, "n": n, "precision": None, "coverage_pct": round(100 * n / len(yte), 2), "avg_ret_pct": None})
            continue
        precision = float(yte[m].mean())  # TP-before-SL rate among taken trades
        avg_ret = float(rte[m].mean())
        curve.append(
            {
                "thr": thr,
                "n": n,
                "precision": round(100 * precision, 2),
                "coverage_pct": round(100 * n / len(yte), 2),
                "avg_ret_pct": round(100 * avg_ret, 4),
                "med_ret_pct": round(100 * float(np.median(rte[m])), 4),
            }
        )

    target = args.target_hit
    cands = [r for r in curve if r["precision"] is not None and r["precision"] >= target]
    best = max(cands, key=lambda r: (r["n"], r["precision"])) if cands else (
        max([r for r in curve if r["precision"] is not None], key=lambda r: (r["precision"], r["n"]))
        if any(r["precision"] is not None for r in curve)
        else None
    )
    reached = bool(cands)

    # unconditional model accuracy (predict outcome)
    test_acc = float((pred == yte).mean())

    model_path = Path(args.model_out)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "mean": mean,
            "std": std,
            "feature_cols": EXT_COLS,
            "tp": args.tp,
            "sl": args.sl,
            "max_bars": args.max_bars,
            "side_mode": args.side_mode,
            "window": args.window,
            "best": best,
        },
        model_path,
    )

    out = {
        "meta": {
            "task": "TP before SL (asymmetric barriers)",
            "tp": args.tp,
            "sl": args.sl,
            "rr": round(args.tp / args.sl, 3),
            "max_bars": args.max_bars,
            "horizon_hours": args.max_bars * 4,
            "side_mode": args.side_mode,
            "timerange": args.timerange,
            "target_precision": target,
            "note": "precision = among trades we TAKE (pred TP-win), share that actually hit TP first",
        },
        "baseline_take_all": {
            "winrate": round(100 * baseline_wr, 2),
            "avg_ret_pct": round(100 * baseline_exp, 4),
            "n": int(len(yte)),
            "mean_pair_wr": round(100 * float(np.mean(base_hits)), 2) if base_hits else None,
        },
        "model_outcome_acc": round(100 * test_acc, 2),
        "val_acc": round(100 * best_va, 2),
        "target_reached": reached,
        "best_operating_point": best,
        "confidence_curve": curve,
        "model_path": str(model_path),
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== Barrier results ===")
    print(f"TP={args.tp*100:.1f}% SL={args.sl*100:.1f}% side={args.side_mode}")
    print(f"baseline take-all WR={out['baseline_take_all']['winrate']}% exp={out['baseline_take_all']['avg_ret_pct']}%")
    print(f"model outcome acc={out['model_outcome_acc']}%")
    for r in curve:
        if r["precision"] is None:
            continue
        mark = " <<<" if best and r == best else ""
        print(
            f"thr={r['thr']:.2f} n={r['n']:6} prec={r['precision']:5.1f}% "
            f"cov={r['coverage_pct']:5.1f}% avg={r['avg_ret_pct']:+.3f}%{mark}"
        )
    print(f"target {target}% reached: {reached}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
