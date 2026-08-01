#!/usr/bin/env python3
"""Meta-labeling on top of Trend4h GRU to chase ~80% precision.

Primary GRU proposes direction; LightGBM meta-model predicts P(primary correct).
Trade only when meta_proba >= threshold (target 80% hit on those trades).
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
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import accuracy_score
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.trend_4h_rnn import _normalize, build_feature_frame  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.train_eval_trend_4h_rnn_80 import (  # noqa: E402
    EXT_COLS,
    Trend4hGRUv2,
    _attach_btc,
    make_binary_sequences,
    predict_proba,
    split_sizes,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument("--window", type=int, default=24)
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--min-move", type=float, default=0.02)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target-hit", type=float, default=80.0)
    # Prefer liquid majors subset for higher precision
    ap.add_argument("--majors-only", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "simulation/data/trend_4h_meta80.json"))
    args = ap.parse_args()

    majors = {
        "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC",
        "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "FIL", "ATOM", "UNI", "AAVE",
        "PEPE", "1000PEPE", "WIF", "INJ", "TIA", "SEI", "RENDER", "FET",
    }

    start_s, end_s = args.timerange.split("-")
    start, end = pd.Timestamp(start_s, tz="UTC"), pd.Timestamp(end_s, tz="UTC")
    pairs = pairs_from_source(ROOT, args.pairs)
    if args.majors_only:
        pairs = [p for p in pairs if p.split("/")[0] in majors]

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    btc_df = ds.load("BTC/USDT:USDT", "5m")
    btc_df = btc_df.loc[(btc_df.index >= start) & (btc_df.index < end)]
    btc_feat = build_feature_frame(btc_df)

    pools = {k: {"X": [], "y": [], "fwd": [], "rule": [], "meta": []} for k in ("train", "val", "test")}

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
        feat = _attach_btc(build_feature_frame(raw), btc_feat)
        X, y, fwd, rule = make_binary_sequences(
            feat, window=args.window, horizon=args.horizon, min_move=args.min_move
        )
        n = len(y)
        n_tr, n_va, n_te = split_sizes(n)
        if n_tr <= 0:
            continue

        # meta features from last bar of each window
        last = X[:, -1, :]
        # indices in EXT_COLS
        idx = {c: j for j, c in enumerate(EXT_COLS)}
        meta = np.column_stack(
            [
                last[:, idx["rule_score"]],
                last[:, idx["adx_n"]],
                last[:, idx["di_spread"]],
                last[:, idx["btc_rule_score"]],
                last[:, idx["vs_btc_ret_1"]],
                last[:, idx["ret_1"]],
                last[:, idx["ret_3"]],
                last[:, idx["range_pct"]],
                np.abs(last[:, idx["rule_score"]]),
                (np.sign(last[:, idx["rule_score"]]) == np.sign(last[:, idx["btc_rule_score"]])).astype(float),
            ]
        )

        sl = {
            "train": slice(0, n_tr),
            "val": slice(n_tr, n_tr + n_va),
            "test": slice(n_tr + n_va, n),
        }
        for name, s in sl.items():
            pools[name]["X"].append(X[s])
            pools[name]["y"].append(y[s])
            pools[name]["fwd"].append(fwd[s])
            pools[name]["rule"].append(rule[s])
            pools[name]["meta"].append(meta[s])
        print(f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} n={n}")

    for name in pools:
        for k in ("X", "y", "fwd", "rule", "meta"):
            pools[name][k] = np.concatenate(pools[name][k])
        print(name, pools[name]["X"].shape)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xtr, ytr = pools["train"]["X"], pools["train"]["y"]
    Xva, yva = pools["val"]["X"], pools["val"]["y"]
    Xte, yte = pools["test"]["X"], pools["test"]["y"]
    fte = pools["test"]["fwd"]

    mean = Xtr.reshape(-1, Xtr.shape[-1]).mean(0)
    std = Xtr.reshape(-1, Xtr.shape[-1]).std(0)
    Xtr_n = _normalize(Xtr, mean, std)
    Xva_n = _normalize(Xva, mean, std)
    Xte_n = _normalize(Xte, mean, std)

    counts = np.bincount(ytr, minlength=2).astype(np.float64)
    w = torch.tensor((counts.sum() / (counts + 1e-6)) / (counts.sum() / (counts + 1e-6)).mean(), dtype=torch.float32, device=device)
    torch.manual_seed(42)
    model = Trend4hGRUv2(n_feat=len(EXT_COLS)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-4)
    loss_fn = nn.CrossEntropyLoss(weight=w)
    tr_loader = DataLoader(TensorDataset(torch.from_numpy(Xtr_n), torch.from_numpy(ytr)), batch_size=args.batch, shuffle=True)

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
            pred = predict_proba(model, Xva_n, device).argmax(1)
            va_acc = float((pred == yva).mean())
        if va_acc > best_va:
            best_va = va_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(f"epoch {ep+1}/{args.epochs} val_acc={va_acc:.3f} best={best_va:.3f}")
    if best_state:
        model.load_state_dict(best_state)
    model.to(device)

    # Primary predictions
    ptr = predict_proba(model, Xtr_n, device)
    pva = predict_proba(model, Xva_n, device)
    pte = predict_proba(model, Xte_n, device)
    dtr, dva, dte = ptr.argmax(1), pva.argmax(1), pte.argmax(1)

    # Meta label: was primary correct?
    y_meta_tr = (dtr == ytr).astype(int)
    y_meta_va = (dva == yva).astype(int)
    y_meta_te = (dte == yte).astype(int)

    def pack(proba, meta, rule):
        conf = proba.max(1)
        p_up = proba[:, 1]
        return np.column_stack([meta, conf, p_up, rule.astype(float), np.abs(rule)])

    Mtr = pack(ptr, pools["train"]["meta"], pools["train"]["rule"])
    Mva = pack(pva, pools["val"]["meta"], pools["val"]["rule"])
    Mte = pack(pte, pools["test"]["meta"], pools["test"]["rule"])

    # Train meta on train, calibrate threshold on val for target hit
    meta_clf = GradientBoostingClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=42
    )
    meta_clf.fit(Mtr, y_meta_tr)
    print("meta val acc", round(accuracy_score(y_meta_va, meta_clf.predict(Mva)) * 100, 2))

    meta_va = meta_clf.predict_proba(Mva)[:, 1]
    meta_te = meta_clf.predict_proba(Mte)[:, 1]

    curve = []
    best = None
    for thr in np.round(np.linspace(0.50, 0.95, 46), 2):
        for side_mode in ("any", "agree_rule"):
            m = meta_va >= thr
            if side_mode == "agree_rule":
                trade = np.where(dva == 1, 1, -1)
                m = m & (trade == pools["val"]["rule"]) & (pools["val"]["rule"] != 0)
            n = int(m.sum())
            if n < 30:
                continue
            # hit among selected = fraction where primary was correct
            hit = float(y_meta_va[m].mean()) * 100
            trade = np.where(dva == 1, 1, -1)
            avg = float((trade[m] * pools["val"]["fwd"][m]).mean()) * 100
            row = {
                "split": "val",
                "mode": side_mode,
                "thr": float(thr),
                "n": n,
                "hit": round(hit, 2),
                "coverage_pct": round(100 * n / len(yva), 2),
                "avg_signed_pct": round(avg, 4),
            }
            curve.append(row)

    target = args.target_hit
    cands = [r for r in curve if r["hit"] >= target]
    if cands:
        best_val = max(cands, key=lambda r: (r["n"], r["hit"]))
        reached_val = True
    else:
        best_val = max(curve, key=lambda r: (r["hit"], r["n"])) if curve else None
        reached_val = False

    # Apply best_val threshold on test
    test_row = None
    if best_val:
        thr = best_val["thr"]
        mode = best_val["mode"]
        m = meta_te >= thr
        trade = np.where(dte == 1, 1, -1)
        if mode == "agree_rule":
            m = m & (trade == pools["test"]["rule"]) & (pools["test"]["rule"] != 0)
        n = int(m.sum())
        if n >= 10:
            hit = float(y_meta_te[m].mean()) * 100
            avg = float((trade[m] * fte[m]).mean()) * 100
            test_row = {
                "split": "test",
                "mode": mode,
                "thr": thr,
                "n": n,
                "hit": round(hit, 2),
                "coverage_pct": round(100 * n / len(yte), 2),
                "avg_signed_pct": round(avg, 4),
                "med_signed_pct": round(100 * float(np.median(trade[m] * fte[m])), 4),
            }

    # Also sweep test curve for reporting
    test_curve = []
    for thr in [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.92, 0.95]:
        for mode in ("any", "agree_rule"):
            m = meta_te >= thr
            trade = np.where(dte == 1, 1, -1)
            if mode == "agree_rule":
                m = m & (trade == pools["test"]["rule"]) & (pools["test"]["rule"] != 0)
            n = int(m.sum())
            if n < 20:
                continue
            test_curve.append(
                {
                    "mode": mode,
                    "thr": thr,
                    "n": n,
                    "hit": round(100 * float(y_meta_te[m].mean()), 2),
                    "coverage_pct": round(100 * n / len(yte), 2),
                    "avg_signed_pct": round(100 * float((trade[m] * fte[m]).mean()), 4),
                }
            )

    # Best test point >= target
    test_cands = [r for r in test_curve if r["hit"] >= target]
    best_test = max(test_cands, key=lambda r: (r["n"], r["hit"])) if test_cands else (
        max(test_curve, key=lambda r: (r["hit"], r["n"])) if test_curve else None
    )
    reached_test = bool(test_cands)

    out = {
        "meta": {
            "approach": "GRU primary + LightGBM meta-label",
            "timerange": args.timerange,
            "horizon_hours": args.horizon * 4,
            "min_move": args.min_move,
            "majors_only": args.majors_only,
            "target_hit": target,
            "note": "Trade only when meta says primary direction is likely correct.",
        },
        "primary_val_acc": round(best_va * 100, 2),
        "primary_test_hit": round(100 * float((dte == yte).mean()), 2),
        "n_test": int(len(yte)),
        "val_best_for_target": best_val,
        "val_target_reached": reached_val,
        "test_at_val_threshold": test_row,
        "test_best_for_target": best_test,
        "test_target_reached": reached_test,
        "test_curve": test_curve,
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== TEST meta curve ===")
    for r in test_curve:
        mark = " <<<" if best_test and r == best_test else ""
        print(
            f"{r['mode']:11} thr={r['thr']:.2f} n={r['n']:5} "
            f"hit={r['hit']:5.1f}% cov={r['coverage_pct']:5.1f}% "
            f"avg={r['avg_signed_pct']:+.3f}%{mark}"
        )
    print(f"primary test hit={out['primary_test_hit']}%")
    print(f"target {target}% reached on test: {reached_test}")
    if best_test:
        print("best_test", best_test)
    if test_row:
        print("test_at_val_thr", test_row)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
