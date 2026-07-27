#!/usr/bin/env python3
"""Train and compare ML models on live_grid dataset; simulate gate @60% / @70%."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
    make_pipeline,
    model_registry,
    time_split,
)
from simulation.scripts.train_grid_model import (  # noqa: E402
    GRID_ID,
    LIVE_GRID_MODEL,
    load_grid_training_trades,
    save_dataset_jsonl,
    train_grid_model,
)
from simulation.ml.per_strategy_models import scenario_meta_path, scenario_model_path  # noqa: E402

OUT_PATH = ROOT / "simulation/results/grid_ml/model_comparison.json"
GATE_THRESHOLDS = (0.6, 0.7)


def _gate_simulation(test_df, pipe, min_conf: float) -> dict[str, Any]:
    features = feature_columns()
    proba = pipe.predict_proba(test_df[features])[:, 1]
    mask = proba >= min_conf
    kept = test_df[mask]
    blocked = test_df[~mask]
    pnl_kept = float(kept["profit_abs"].sum()) if len(kept) else 0.0
    pnl_blocked = float(blocked["profit_abs"].sum()) if len(blocked) else 0.0
    wins = int((kept["label"] == 1).sum()) if len(kept) else 0
    return {
        "min_confidence": min_conf,
        "trades": int(len(kept)),
        "blocked": int(len(blocked)),
        "pnl_usdt": round(pnl_kept, 4),
        "blocked_pnl_usdt": round(pnl_blocked, 4),
        "wins": wins,
        "losses": int(len(kept) - wins),
        "win_rate": round(wins / len(kept), 4) if len(kept) else None,
    }


def compare_grid_models(
    root: Path,
    models: list[str] | None = None,
    *,
    min_trades: int = 30,
) -> dict[str, Any]:
    trades = load_grid_training_trades(root)
    wins = sum(1 for t in trades if float(t.get("profit_abs") or 0) >= 0)
    losses = len(trades) - wins
    print(f"Grid dataset: {len(trades)} trades · W{wins} L{losses}")

    save_dataset_jsonl(trades, root / "simulation/results/grid_ml/dataset.jsonl")

    if len(trades) < min_trades:
        raise RuntimeError(f"not enough grid trades ({len(trades)} < {min_trades})")

    market_store = make_market_store(root)
    df = build_dataframe(trades, market_store)
    df["profit_abs"] = [float(t.get("profit_abs") or 0) for t in trades]
    train_df, test_df = time_split(df)
    calibrate = len(df) >= 120
    registry = model_registry()
    models = models or list(registry.keys())

    rows: list[dict[str, Any]] = []
    for name in models:
        label = f"{name} + {'isotonic' if calibrate else 'raw'}"
        print(f"--- train {label} ---")
        try:
            pipe = make_pipeline(name, calibrate=calibrate)
            metrics = evaluate_pipeline(pipe, train_df, test_df)
            gate_sims = [_gate_simulation(test_df, pipe, th) for th in GATE_THRESHOLDS]
            rows.append(
                {
                    "model": name,
                    "label": label,
                    "calibrated": calibrate,
                    **metrics,
                    "gate_simulation": gate_sims,
                    "error": None,
                }
            )
        except Exception as exc:
            rows.append({"model": name, "label": label, "error": str(exc)})

    ok = [r for r in rows if not r.get("error")]
    ok.sort(
        key=lambda r: (
            max((g.get("pnl_usdt") or 0) for g in r.get("gate_simulation", [])),
            r.get("roc_auc") or 0,
            r.get("loss_recall") or 0,
        ),
        reverse=True,
    )
    best = ok[0] if ok else None

    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "scenario_id": GRID_ID,
        "n_trades": len(df),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "market_coverage": market_store.coverage(df),
        "gate_thresholds": list(GATE_THRESHOLDS),
        "ranking": ok,
        "all_results": rows,
        "best_model": best["model"] if best else None,
        "note": "Ranked by max gate PnL (@60/@70), then ROC-AUC, then loss_recall",
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _print_table(result: dict) -> None:
    print(f"\nDataset: {result['n_trades']} trades (train {result['n_train']}, test {result['n_test']})")
    print(f"\n{'Model':<16} {'AUC':>6} {'Acc':>6} {'ProfR':>6} {'@60%PnL':>8} {'@70%PnL':>8} {'@70%N':>6}")
    print("-" * 68)
    for row in result.get("ranking", []):
        g60 = next((g for g in row.get("gate_simulation", []) if g["min_confidence"] == 0.6), {})
        g70 = next((g for g in row.get("gate_simulation", []) if g["min_confidence"] == 0.7), {})
        print(
            f"{row['model']:<16} "
            f"{row.get('roc_auc') or 0:>6.3f} "
            f"{row.get('accuracy') or 0:>5.1%} "
            f"{row.get('profit_recall') or 0:>5.1%} "
            f"{g60.get('pnl_usdt', 0):>8.2f} "
            f"{g70.get('pnl_usdt', 0):>8.2f} "
            f"{g70.get('trades', 0):>6}"
        )
    for row in result.get("all_results", []):
        if row.get("error"):
            print(f"{row['model']:<16} ERROR: {row['error']}")
    if result.get("best_model"):
        print(f"\nBest: {result['best_model']}")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Compare grid ML models")
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--deploy-best", action="store_true", help="Train and deploy best model")
    args = ap.parse_args()

    result = compare_grid_models(ROOT, models=args.models, min_trades=args.min_trades)
    _print_table(result)
    print(f"\nReport: {OUT_PATH}")

    if args.deploy_best and result.get("best_model"):
        print(f"\n=== Deploy best: {result['best_model']} ===")
        meta = train_grid_model(ROOT, model_name=result["best_model"], min_trades=args.min_trades)
        print(f"  sim: {scenario_model_path(ROOT, GRID_ID)}")
        print(f"  live: {LIVE_GRID_MODEL / 'pnl_classifier.joblib'}")
        print(f"  ROC {meta.get('roc_auc')} · profit_recall {meta.get('profit_recall')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
