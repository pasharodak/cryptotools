#!/usr/bin/env python3
"""Train + evaluate 4H Trend GRU vs rule-based Trend4hModel."""
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
from simulation.ml.trend_4h_rnn import (  # noqa: E402
    FEATURE_COLS,
    build_feature_frame,
    make_sequences,
)
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402


def _split_sizes(n: int, val_frac: float, test_frac: float) -> tuple[int, int, int]:
    n_test = max(1, int(n * test_frac))
    n_val = max(1, int(n * val_frac))
    n_train = n - n_val - n_test
    if n_train < 20:
        return 0, 0, 0
    return n_train, n_val, n_test


def collect_splits(
    pairs: list[str],
    *,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    window: int,
    horizon: int,
    flat_band: float,
    limit: int,
    val_frac: float,
    test_frac: float,
) -> dict[str, dict[str, np.ndarray]]:
    """Per-pair time split, then concatenate train/val/test pools."""
    ds = HistoricalDatastore(ROOT / "simulation/data/freqtrade")
    rule = Trend4hModel()
    pools = {
        "train": {"X": [], "y": [], "fwd": []},
        "val": {"X": [], "y": [], "fwd": []},
        "test": {"X": [], "y": [], "fwd": []},
        "test_rule_dir": [],
        "test_rule_fwd": [],
    }

    for i, pair in enumerate(pairs, 1):
        if limit and i > limit:
            break
        try:
            df = ds.load(pair, timeframe)
        except FileNotFoundError:
            print(f"[{i}] SKIP no data {pair}")
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if len(df) < 3000:
            print(f"[{i}] SKIP short {pair}")
            continue

        feat = build_feature_frame(df)
        X, y, fwd = make_sequences(feat, window=window, horizon=horizon, flat_band=flat_band)
        n = len(y)
        n_train, n_val, n_test = _split_sizes(n, val_frac, test_frac)
        if n_train <= 0:
            print(f"[{i}] SKIP thin {pair} n={n}")
            continue

        slices = {
            "train": slice(0, n_train),
            "val": slice(n_train, n_train + n_val),
            "test": slice(n_train + n_val, n),
        }
        for name, sl in slices.items():
            pools[name]["X"].append(X[sl])
            pools[name]["y"].append(y[sl])
            pools[name]["fwd"].append(fwd[sl])

        # Rule dirs aligned to same sequence end bars on dropped feature frame
        h4 = rule.build_4h_frame(df)
        # rebuild alignment: make_sequences drops NaN rows from feat
        feat2 = feat.dropna(subset=FEATURE_COLS + ["close"])
        # map each sequence end index in feat2 to h4 by timestamp
        ends = feat2.index[window - 1 : len(feat2) - horizon]
        # ends length == n
        test_ends = ends[n_train + n_val :]
        for ts, fret in zip(test_ends, fwd[n_train + n_val :]):
            if ts not in h4.index:
                # nearest prior bar
                loc = h4.index.searchsorted(ts, side="right") - 1
                if loc < 0:
                    continue
                row = h4.iloc[loc]
            else:
                row = h4.loc[ts]
            d = int(row["trend_4h_dir"])
            if d == 0:
                continue
            pools["test_rule_dir"].append(d)
            pools["test_rule_fwd"].append(float(fret))

        print(f"[{i}/{len(pairs)}] {pair.split('/')[0]:12} n={n} train={n_train} test={n_test}")

    out = {}
    for name in ("train", "val", "test"):
        if not pools[name]["X"]:
            raise SystemExit(f"empty {name} pool")
        out[name] = {
            "X": np.concatenate(pools[name]["X"]),
            "y": np.concatenate(pools[name]["y"]),
            "fwd": np.concatenate(pools[name]["fwd"]),
        }
    out["rule_test"] = {
        "dir": np.asarray(pools["test_rule_dir"], dtype=np.int64),
        "fwd": np.asarray(pools["test_rule_fwd"], dtype=np.float64),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--timerange", default="20260101-20260720")
    ap.add_argument("--window", type=int, default=24, help="4h bars (~4d)")
    ap.add_argument("--horizon", type=int, default=6, help="4h bars ahead (6=24h)")
    ap.add_argument("--flat-band", type=float, default=0.002)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "simulation/data/trend_4h_rnn_eval.json"))
    ap.add_argument("--model-out", default=str(ROOT / "simulation/data/models/trend_4h_gru.pt"))
    args = ap.parse_args()

    start_s, end_s = args.timerange.split("-")
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC")
    pairs = pairs_from_source(ROOT, args.pairs)

    print(f"GRU 4h · window={args.window} · horizon={args.horizon} ({args.horizon*4}h) · {args.timerange}")
    pools = collect_splits(
        pairs,
        timeframe=args.timeframe,
        start=start,
        end=end,
        window=args.window,
        horizon=args.horizon,
        flat_band=args.flat_band,
        limit=args.limit,
        val_frac=0.15,
        test_frac=0.15,
    )
    Xtr, ytr, ftr = pools["train"]["X"], pools["train"]["y"], pools["train"]["fwd"]
    Xva, yva, fva = pools["val"]["X"], pools["val"]["y"], pools["val"]["fwd"]
    Xte, yte, fte = pools["test"]["X"], pools["test"]["y"], pools["test"]["fwd"]
    print(
        f"train={len(ytr)} val={len(yva)} test={len(yte)} "
        f"y_train={np.bincount(ytr, minlength=3).tolist()}"
    )

    # Concatenate in time-order within each pair already done; for trainer API
    # pass train+val+test stacked so its internal split matches our sizes.
    # Better: call lower-level train with our splits.
    from simulation.ml.trend_4h_rnn import (  # noqa: WPS433
        Trend4hGRU,
        _normalize,
        evaluate_predictions,
    )
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    mean = Xtr.reshape(-1, Xtr.shape[-1]).mean(axis=0)
    std = Xtr.reshape(-1, Xtr.shape[-1]).std(axis=0)
    Xtr_n, Xva_n, Xte_n = _normalize(Xtr, mean, std), _normalize(Xva, mean, std), _normalize(Xte, mean, std)

    counts = np.bincount(ytr, minlength=3).astype(np.float64)
    weights = counts.sum() / (counts + 1e-6)
    weights = weights / weights.mean()
    w = torch.tensor(weights, dtype=torch.float32, device=device)

    torch.manual_seed(42)
    model = Trend4hGRU().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss(weight=w)
    tr_loader = DataLoader(TensorDataset(torch.from_numpy(Xtr_n), torch.from_numpy(ytr)), batch_size=args.batch, shuffle=True)
    va_loader = DataLoader(TensorDataset(torch.from_numpy(Xva_n), torch.from_numpy(yva)), batch_size=args.batch)
    te_loader = DataLoader(TensorDataset(torch.from_numpy(Xte_n), torch.from_numpy(yte)), batch_size=args.batch)

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
    gru_metrics = evaluate_predictions(model, te_loader, device, yte, fte)
    gru_metrics["val_acc"] = round(best_va, 4)
    gru_metrics["n_train"] = int(len(ytr))
    gru_metrics["n_val"] = int(len(yva))
    gru_metrics["n_test"] = int(len(yte))
    print("GRU:", json.dumps(gru_metrics, indent=2))

    rd, rf = pools["rule_test"]["dir"], pools["rule_test"]["fwd"]
    if len(rd):
        signed = rd * rf
        hit = ((rf > 0) & (rd > 0)) | ((rf < 0) & (rd < 0))
        rule_metrics = {
            "n": int(len(rd)),
            "hit_rate": round(100 * float(hit.mean()), 2),
            "avg_signed_pct": round(100 * float(signed.mean()), 4),
            "med_signed_pct": round(100 * float(np.median(signed)), 4),
        }
    else:
        rule_metrics = {"n": 0}
    print("Rule:", json.dumps(rule_metrics, indent=2))

    model_path = Path(args.model_out)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "mean": mean,
            "std": std,
            "feature_cols": FEATURE_COLS,
            "window": args.window,
            "horizon": args.horizon,
            "flat_band": args.flat_band,
            "metrics": gru_metrics,
        },
        model_path,
    )

    delta = None
    if gru_metrics.get("trade_hit_rate") is not None and rule_metrics.get("hit_rate") is not None:
        delta = round(gru_metrics["trade_hit_rate"] - rule_metrics["hit_rate"], 2)

    out = {
        "meta": {
            "model": "Trend4hGRU",
            "timerange": args.timerange,
            "window_bars": args.window,
            "horizon_bars": args.horizon,
            "horizon_hours": args.horizon * 4,
            "features": FEATURE_COLS,
            "device": str(device),
            "split": "per-pair time-ordered 70/15/15",
        },
        "gru": gru_metrics,
        "rule_based": rule_metrics,
        "delta_hit_pp": delta,
        "model_path": str(model_path),
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}")
    print(f"Saved {model_path}")
    if delta is not None:
        print(f"GRU − rule hit: {delta:+.2f} pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
