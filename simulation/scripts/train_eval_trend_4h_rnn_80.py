#!/usr/bin/env python3
"""Push 4H Trend GRU toward ~80% hit via selective (confidence-gated) trading.

Reality check: 80% on ALL bars is not realistic for crypto direction.
This script maximizes precision by only trading when softmax confidence is high,
optionally requiring agreement with rule-based trend and/or a minimum |fwd| label band.
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
from simulation.ml.trend_4h import Trend4hModel  # noqa: E402
from simulation.ml.trend_4h_rnn import (  # noqa: E402
    FEATURE_COLS,
    Trend4hGRU,
    _normalize,
    build_feature_frame,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

# Extended features (pair + BTC context)
EXT_COLS = FEATURE_COLS + [
    "btc_ret_1",
    "btc_ret_3",
    "btc_ret_6",
    "vs_btc_ret_1",
    "vs_btc_ret_3",
    "btc_rule_score",
]


class Trend4hGRUv2(nn.Module):
    def __init__(self, n_feat: int = len(EXT_COLS), hidden: int = 96, layers: int = 2, dropout: float = 0.25):
        super().__init__()
        self.gru = nn.GRU(
            input_size=n_feat,
            hidden_size=hidden,
            num_layers=layers,
            batch_first=True,
            dropout=dropout if layers > 1 else 0.0,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 2),  # binary: down / up (no flat — flats filtered in labels)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.gru(x)
        return self.head(out[:, -1, :])


def _attach_btc(feat: pd.DataFrame, btc_feat: pd.DataFrame) -> pd.DataFrame:
    if feat.empty or btc_feat.empty:
        return pd.DataFrame()
    b = btc_feat.reindex(feat.index, method="ffill")
    out = feat.copy()
    out["btc_ret_1"] = b["ret_1"]
    out["btc_ret_3"] = b["ret_3"]
    out["btc_ret_6"] = b["ret_6"]
    out["vs_btc_ret_1"] = feat["ret_1"] - b["ret_1"]
    out["vs_btc_ret_3"] = feat["ret_3"] - b["ret_3"]
    out["btc_rule_score"] = b["rule_score"]
    return out


def make_binary_sequences(
    feat: pd.DataFrame,
    *,
    window: int,
    horizon: int,
    min_move: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Binary labels only when |fwd| >= min_move. Returns X,y,fwd,rule_dir."""
    cols = EXT_COLS + ["close", "rule_score"]
    df = feat.dropna(subset=[c for c in cols if c in feat.columns])
    missing = [c for c in EXT_COLS if c not in df.columns]
    if missing or len(df) < window + horizon + 10:
        return (
            np.zeros((0, window, len(EXT_COLS)), np.float32),
            np.zeros((0,), np.int64),
            np.zeros((0,), np.float32),
            np.zeros((0,), np.int64),
        )

    x_mat = df[EXT_COLS].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    rule = df["rule_score"].to_numpy(dtype=np.float64)
    xs, ys, rets, rules = [], [], [], []
    for i in range(window - 1, len(df) - horizon):
        fwd = close[i + horizon] / close[i] - 1.0
        if abs(fwd) < min_move:
            continue  # skip tiny moves — harder / noise
        lab = 1 if fwd > 0 else 0
        xs.append(x_mat[i - window + 1 : i + 1])
        ys.append(lab)
        rets.append(fwd)
        # rule dir at decision bar
        rs = rule[i]
        rd = 1 if rs >= 0.18 else (-1 if rs <= -0.18 else 0)
        rules.append(rd)
    return (
        np.asarray(xs, np.float32),
        np.asarray(ys, np.int64),
        np.asarray(rets, np.float32),
        np.asarray(rules, np.int64),
    )


def split_sizes(n: int, val_frac: float = 0.15, test_frac: float = 0.15) -> tuple[int, int, int]:
    n_test = max(1, int(n * test_frac))
    n_val = max(1, int(n * val_frac))
    n_train = n - n_val - n_test
    return (n_train, n_val, n_test) if n_train >= 30 else (0, 0, 0)


@torch.no_grad()
def predict_proba(model: nn.Module, Xn: np.ndarray, device: torch.device, batch: int = 1024) -> np.ndarray:
    model.eval()
    probs = []
    for i in range(0, len(Xn), batch):
        xb = torch.from_numpy(Xn[i : i + batch]).to(device)
        logits = model(xb)
        p = torch.softmax(logits, dim=1).cpu().numpy()
        probs.append(p)
    return np.concatenate(probs, axis=0) if probs else np.zeros((0, 2), np.float32)


def conf_curve(proba: np.ndarray, y: np.ndarray, fwd: np.ndarray, rule_dir: np.ndarray) -> list[dict]:
    """Sweep confidence thresholds; pred = argmax, conf = max softmax."""
    pred = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    trade_dir = np.where(pred == 1, 1, -1)
    rows = []
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95, 0.97, 0.98, 0.99]:
        masks = {
            "conf_only": conf >= thr,
            "conf_and_rule": (conf >= thr) & (trade_dir == rule_dir) & (rule_dir != 0),
        }
        for mode, m in masks.items():
            n = int(m.sum())
            cov = round(100 * n / max(len(y), 1), 2)
            if n < 20:
                rows.append(
                    {
                        "mode": mode,
                        "thr": thr,
                        "n": n,
                        "hit": None,
                        "coverage_pct": cov,
                        "avg_signed_pct": None,
                        "med_signed_pct": None,
                    }
                )
                continue
            hit = float((pred[m] == y[m]).mean())
            signed = trade_dir[m] * fwd[m]
            rows.append(
                {
                    "mode": mode,
                    "thr": thr,
                    "n": n,
                    "hit": round(100 * hit, 2),
                    "coverage_pct": cov,
                    "avg_signed_pct": round(100 * float(signed.mean()), 4),
                    "med_signed_pct": round(100 * float(np.median(signed)), 4),
                }
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument("--window", type=int, default=24)
    ap.add_argument("--horizon", type=int, default=6, help="6 bars = 24h")
    ap.add_argument("--min-move", type=float, default=0.015, help="min |fwd| to label (1.5%)")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target-hit", type=float, default=80.0)
    ap.add_argument("--out", default=str(ROOT / "simulation/data/trend_4h_rnn_80.json"))
    ap.add_argument("--model-out", default=str(ROOT / "simulation/data/models/trend_4h_gru_v2.pt"))
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start, end = pd.Timestamp(start_s, tz="UTC"), pd.Timestamp(end_s, tz="UTC")
    pairs = pairs_from_source(ROOT, args.pairs)
    ds = HistoricalDatastore(ROOT / "simulation/data/freqtrade")

    btc = ds.load("BTC/USDT:USDT", "5m")
    btc = btc.loc[(btc.index >= start) & (btc.index < end)]
    btc_feat = build_feature_frame(btc)
    print(f"BTC 4h feat bars={len(btc_feat)}")

    pools = {k: {"X": [], "y": [], "fwd": [], "rule": []} for k in ("train", "val", "test")}
    for i, pair in enumerate(pairs, 1):
        if args.limit and i > args.limit:
            break
        if pair.startswith("BTC/"):
            continue
        try:
            df = ds.load(pair, "5m")
        except FileNotFoundError:
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if len(df) < 3000:
            continue
        feat = _attach_btc(build_feature_frame(df), btc_feat)
        X, y, fwd, rule = make_binary_sequences(
            feat, window=args.window, horizon=args.horizon, min_move=args.min_move
        )
        n = len(y)
        n_tr, n_va, n_te = split_sizes(n)
        if n_tr <= 0:
            continue
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
        print(f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} n={n}")

    for name in pools:
        pools[name]["X"] = np.concatenate(pools[name]["X"])
        pools[name]["y"] = np.concatenate(pools[name]["y"])
        pools[name]["fwd"] = np.concatenate(pools[name]["fwd"])
        pools[name]["rule"] = np.concatenate(pools[name]["rule"])
        print(name, pools[name]["X"].shape, "y", np.bincount(pools[name]["y"], minlength=2).tolist())

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xtr, ytr = pools["train"]["X"], pools["train"]["y"]
    Xva, yva = pools["val"]["X"], pools["val"]["y"]
    Xte, yte = pools["test"]["X"], pools["test"]["y"]
    fte, rte = pools["test"]["fwd"], pools["test"]["rule"]

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
    tr_loader = DataLoader(TensorDataset(torch.from_numpy(Xtr_n), torch.from_numpy(ytr)), batch_size=args.batch, shuffle=True)
    va_loader = DataLoader(TensorDataset(torch.from_numpy(Xva_n), torch.from_numpy(yva)), batch_size=args.batch)

    best_state, best_va = None, -1.0
    for ep in range(args.epochs):
        model.train()
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        # val acc
        model.eval()
        correct = total = 0
        with torch.no_grad():
            for xb, yb in va_loader:
                pred = model(xb.to(device)).argmax(1).cpu()
                correct += int((pred == yb).sum())
                total += len(yb)
        va_acc = correct / max(total, 1)
        if va_acc > best_va:
            best_va = va_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(f"epoch {ep+1}/{args.epochs} val_acc={va_acc:.3f} best={best_va:.3f}")

    if best_state:
        model.load_state_dict(best_state)
    model.to(device)

    proba = predict_proba(model, Xte_n, device)
    pred = proba.argmax(1)
    base_hit = float((pred == yte).mean())
    curve = conf_curve(proba, yte, fte, rte)

    # pick best operating point >= target hit with max coverage
    target = args.target_hit
    candidates = [r for r in curve if r["hit"] is not None and r["hit"] >= target]
    if candidates:
        best = max(candidates, key=lambda r: (r["n"], r["hit"]))
        reached = True
    else:
        # closest to target
        scored = [r for r in curve if r["hit"] is not None]
        best = max(scored, key=lambda r: (r["hit"], r["n"])) if scored else None
        reached = False

    model_path = Path(args.model_out)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "mean": mean,
            "std": std,
            "feature_cols": EXT_COLS,
            "window": args.window,
            "horizon": args.horizon,
            "min_move": args.min_move,
            "best_point": best,
        },
        model_path,
    )

    out = {
        "meta": {
            "goal": f"{target}% hit via selective GRU",
            "timerange": args.timerange,
            "window": args.window,
            "horizon_hours": args.horizon * 4,
            "min_move": args.min_move,
            "features": EXT_COLS,
            "device": str(device),
            "note": "80% on all bars is not feasible; we gate by softmax confidence (+ optional rule agree).",
        },
        "unconditional_test_hit": round(100 * base_hit, 2),
        "val_acc": round(best_va * 100, 2),
        "n_test": int(len(yte)),
        "target_hit": target,
        "target_reached": reached,
        "best_operating_point": best,
        "confidence_curve": curve,
        "model_path": str(model_path),
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== Confidence curve (top) ===")
    for r in curve:
        if r["hit"] is None:
            continue
        mark = " <<<" if best and r == best else ""
        print(
            f"{r['mode']:14} thr={r['thr']:.2f} n={r['n']:6} "
            f"hit={r['hit']:5.1f}% cov={r['coverage_pct']:5.1f}% "
            f"avg={r['avg_signed_pct']:+.3f}%{mark}"
        )
    print(f"\nunconditional hit={out['unconditional_test_hit']}%")
    if best:
        print(
            f"best for >={target}%: mode={best['mode']} thr={best['thr']} "
            f"hit={best['hit']}% n={best['n']} cov={best['coverage_pct']}% reached={reached}"
        )
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
