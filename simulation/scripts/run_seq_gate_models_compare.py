#!/usr/bin/env python3
"""Sequential compare of gate sequence models on strategy trade-cache.

Models: tiny_transformer, patch_tst, itransformer, gru, bilstm_attn, modern_tcn, ensemble.
Train/val from cache train split; test from cache test split (april-cut caches).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.barrier_transformer import threshold_metrics  # noqa: E402
from simulation.ml.seq_gate_models import register_builders, train_one  # noqa: E402
from simulation.scripts.train_eval_barrier_transformer import (  # noqa: E402
    GATE_SCENARIOS,
    _load_gate_rows,
    collect_gate_pool,
    time_split_train_val,
)

BTC_PAIR = "BTC/USDT:USDT"
OUT_DIR = ROOT / "simulation/results/seq_gate_compare"
CACHE_NPZ = OUT_DIR / "gate_pools_w64.npz"


def _load_or_build_pools(window: int, scenarios: list[str], force: bool = False):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_NPZ.is_file() and not force:
        blob = np.load(CACHE_NPZ, allow_pickle=False)
        print(f"loaded cached pools {CACHE_NPZ} train={len(blob['y_tr'])} test={len(blob['y_te'])}")
        return (
            {"X": blob["X_tr"], "y": blob["y_tr"], "realized": blob["r_tr"], "times": blob["t_tr"]},
            {"X": blob["X_te"], "y": blob["y_te"], "realized": blob["r_te"], "times": blob["t_te"]},
        )

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    try:
        btc = ds.load(BTC_PAIR, "5m")
    except FileNotFoundError:
        btc = None

    train_rows = _load_gate_rows(scenarios, "train")
    test_rows = _load_gate_rows(scenarios, "test")
    train_pool = collect_gate_pool(train_rows, window=window, btc_full=btc)
    test_pool = collect_gate_pool(test_rows, window=window, btc_full=btc)
    np.savez_compressed(
        CACHE_NPZ,
        X_tr=train_pool["X"],
        y_tr=train_pool["y"],
        r_tr=train_pool["realized"],
        t_tr=train_pool["times"],
        X_te=test_pool["X"],
        y_te=test_pool["y"],
        r_te=test_pool["realized"],
        t_te=test_pool["times"],
    )
    print(f"saved pools -> {CACHE_NPZ}")
    return train_pool, test_pool


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=64)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--subsample-cap", type=int, default=60_000)
    ap.add_argument("--models", default="tiny_transformer,patch_tst,itransformer,gru,bilstm_attn,modern_tcn")
    ap.add_argument("--gate-scenarios", default=",".join(GATE_SCENARIOS))
    ap.add_argument("--force-rebuild-cache", action="store_true")
    ap.add_argument("--stake", type=float, default=15.0)
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.gate_scenarios.split(",") if s.strip()]
    names = [s.strip() for s in args.models.split(",") if s.strip()]

    train_pool, test_pool = _load_or_build_pools(args.window, scenarios, force=args.force_rebuild_cache)
    if args.subsample_cap and len(train_pool["y"]) > args.subsample_cap:
        step = max(1, len(train_pool["y"]) // args.subsample_cap)
        train_pool = {k: v[::step][: args.subsample_cap] for k, v in train_pool.items()}
        print(f"subsampled train n={len(train_pool['y'])}")

    tr, va = time_split_train_val(train_pool, val_frac=0.10)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device} n_feat={tr['X'].shape[-1]} T={tr['X'].shape[1]}")

    builders = register_builders(seq_len=tr["X"].shape[1], n_feat=tr["X"].shape[-1])
    results = []
    proba_map: dict[str, np.ndarray] = {}

    for name in names:
        if name not in builders:
            print(f"SKIP unknown model {name}")
            continue
        print(f"\n=== {name} ===", flush=True)
        try:
            model = builders[name]()
            res = train_one(
                name,
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
                lr=args.lr,
                device=device,
            )
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            results.append({"name": name, "error": str(exc)})
            continue
        proba_map[name] = res.proba_test
        row = {
            "name": name,
            "n_params": res.n_params,
            "thr": res.thr,
            "temperature": round(res.temperature, 4),
            "val_auc": res.val_metrics["auc"],
            "test_auc": res.test_metrics["auc"],
            "test_precision": res.test_metrics["at_thr"]["precision"],
            "test_coverage": res.test_metrics["at_thr"]["coverage"],
            "test_n": res.test_metrics["at_thr"]["n"],
            "test_pnl_usdt": res.test_metrics["at_thr"]["pnl_usdt"],
            "test_avg_ret": res.test_metrics["at_thr"]["avg_ret"],
            "device": res.device,
        }
        results.append(row)
        print(json.dumps(row, indent=2), flush=True)

    # Ensemble: mean of available test probas, thr from averaging then pick on val isn't available —
    # use mean of member thrs and evaluate on test.
    if len(proba_map) >= 2:
        print("\n=== ensemble_mean ===", flush=True)
        stack = np.vstack(list(proba_map.values()))
        proba_e = stack.mean(axis=0)
        thr_e = float(np.mean([r["thr"] for r in results if "thr" in r]))
        at = threshold_metrics(test_pool["y"], proba_e, test_pool["realized"], thr=thr_e, stake=args.stake)
        try:
            from sklearn.metrics import roc_auc_score

            auc_e = float(roc_auc_score(test_pool["y"], proba_e))
        except Exception:
            auc_e = None
        ens = {
            "name": "ensemble_mean",
            "n_params": sum(r.get("n_params") or 0 for r in results if "n_params" in r),
            "thr": thr_e,
            "temperature": None,
            "val_auc": None,
            "test_auc": None if auc_e is None else round(auc_e, 4),
            "test_precision": at["precision"],
            "test_coverage": at["coverage"],
            "test_n": at["n"],
            "test_pnl_usdt": at["pnl_usdt"],
            "test_avg_ret": at["avg_ret"],
            "device": str(device),
            "members": list(proba_map.keys()),
        }
        results.append(ens)
        print(json.dumps(ens, indent=2), flush=True)

    # Rank by test pnl then precision
    scored = [r for r in results if "test_pnl_usdt" in r]
    scored.sort(key=lambda r: (r["test_pnl_usdt"] or -1e9, r["test_precision"] or 0), reverse=True)

    report = {
        "created": datetime.now(UTC).isoformat(),
        "config": {
            "window": args.window,
            "epochs": args.epochs,
            "batch": args.batch,
            "lr": args.lr,
            "subsample_cap": args.subsample_cap,
            "scenarios": scenarios,
            "device": str(device),
        },
        "leaderboard": scored,
        "all_results": results,
    }
    out = OUT_DIR / "summary.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Markdown table
    lines = [
        "# Seq gate models compare",
        "",
        f"Created: {report['created']}",
        "",
        "| rank | model | params | test AUC | thr | precision | coverage | n | PnL USDT | avg_ret |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(scored, 1):
        lines.append(
            f"| {i} | `{r['name']}` | {r.get('n_params','')} | {r.get('test_auc')} | {r.get('thr')} | "
            f"{r.get('test_precision')} | {r.get('test_coverage')} | {r.get('test_n')} | "
            f"{r.get('test_pnl_usdt')} | {r.get('test_avg_ret')} |"
        )
    md = OUT_DIR / "summary.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nwrote {out}\nwrote {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
