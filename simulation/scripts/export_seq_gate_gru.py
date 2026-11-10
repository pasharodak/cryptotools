#!/usr/bin/env python3
"""Retrain seq-gate GRU from cached pools and save live checkpoint."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.barrier_transformer import FEATURE_COLS  # noqa: E402
from simulation.ml.seq_gate_models import (  # noqa: E402
    register_builders,
    save_seq_gate_checkpoint,
    train_one,
)
from simulation.scripts.train_eval_barrier_transformer import time_split_train_val  # noqa: E402

CACHE = ROOT / "simulation/results/seq_gate_compare/gate_pools_w64.npz"
OUT_SIM = ROOT / "simulation/data/models/seq_gate_gru.pt"
OUT_SITE = ROOT / "site/user_data/models/seq_gate_gru/seq_gate_gru.pt"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--window", type=int, default=64)
    ap.add_argument("--subsample-cap", type=int, default=60_000)
    ap.add_argument("--out", type=Path, default=OUT_SIM)
    args = ap.parse_args()

    if not CACHE.is_file():
        raise SystemExit(f"missing cache {CACHE} — run run_seq_gate_models_compare.py first")

    blob = np.load(CACHE, allow_pickle=False)
    train_pool = {"X": blob["X_tr"], "y": blob["y_tr"], "realized": blob["r_tr"], "times": blob["t_tr"]}
    test_pool = {"X": blob["X_te"], "y": blob["y_te"], "realized": blob["r_te"], "times": blob["t_te"]}
    if args.subsample_cap and len(train_pool["y"]) > args.subsample_cap:
        step = max(1, len(train_pool["y"]) // args.subsample_cap)
        train_pool = {k: v[::step][: args.subsample_cap] for k, v in train_pool.items()}
        print(f"subsampled train n={len(train_pool['y'])}")

    tr, va = time_split_train_val(train_pool, val_frac=0.10)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device} n_feat={tr['X'].shape[-1]} T={tr['X'].shape[1]}")
    builders = register_builders(seq_len=tr["X"].shape[1], n_feat=tr["X"].shape[-1])
    model = builders["gru"]()
    res = train_one(
        "gru",
        model,
        tr["X"],
        tr["y"],
        va["X"],
        va["y"],
        va["realized"],
        test_pool["X"],
        test_pool["y"],
        test_pool["realized"],
        epochs=args.epochs,
        batch=args.batch,
        device=device,
    )
    path = save_seq_gate_checkpoint(
        args.out,
        res,
        arch="gru",
        window=args.window,
        feature_cols=list(FEATURE_COLS),
        extra_meta={"source": "seq_gate_compare", "leaderboard_note": "PnL leader GRU"},
    )
    print(f"saved {path}")
    print(json.dumps({"thr": res.thr, "temp": res.temperature, "test": res.test_metrics}, indent=2))

    OUT_SITE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, OUT_SITE)
    meta_src = path.with_suffix(".meta.json")
    if meta_src.is_file():
        shutil.copy2(meta_src, OUT_SITE.with_suffix(".meta.json"))
    print(f"copied -> {OUT_SITE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
