#!/usr/bin/env python3
"""Compare profit/loss classifiers and optionally deploy the best one."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import compare_models, score_all, train_model  # noqa: E402


def _print_table(result: dict) -> None:
    print(f"\nDataset: {result['n_trades']} trades (train {result['n_train']}, test {result['n_test']})")
    cov = result.get("market_coverage", {})
    if cov:
        print(f"Market coverage: {cov.get('with_market', 0)}/{cov.get('total', 0)}")
    print("\nRanked by ROC-AUC (then loss recall):\n")
    print(f"{'Model':<16} {'AUC':>6} {'Acc':>6} {'LossR':>6} {'LossP':>6} {'ProfR':>6} {'F1':>6}")
    print("-" * 58)
    for row in result.get("ranking", []):
        print(
            f"{row['model']:<16} "
            f"{row.get('roc_auc') or 0:>6.3f} "
            f"{row.get('accuracy') or 0:>5.1%} "
            f"{row.get('loss_recall') or 0:>5.1%} "
            f"{row.get('loss_precision') or 0:>5.1%} "
            f"{row.get('profit_recall') or 0:>5.1%} "
            f"{row.get('macro_f1') or 0:>6.3f}"
        )
    for row in result.get("all_results", []):
        if row.get("error"):
            print(f"{row['model']:<16} ERROR: {row['error']}")
    best = result.get("best_model")
    if best:
        print(f"\nBest: {best}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare PnL classifiers")
    ap.add_argument("--deploy-best", action="store_true", help="Train and save best model to production path")
    ap.add_argument("--models", nargs="*", default=None, help="Subset of model keys")
    args = ap.parse_args()

    result = compare_models(ROOT, models=args.models)
    _print_table(result)
    out = ROOT / "simulation/results/trade_db/models/model_comparison.json"
    print(f"\nReport: {out}")

    if args.deploy_best and result.get("best_model"):
        best = result["best_model"]
        print(f"\n=== Deploy best model: {best} ===")
        metrics = train_model(ROOT, model_name=best)
        scored = score_all(ROOT)
        print(f"saved: {ROOT / 'simulation/results/trade_db/models/pnl_classifier.joblib'}")
        print(f"accuracy (test): {metrics['accuracy']:.1%} · ROC-AUC: {metrics.get('roc_auc', 0):.3f}")
        print(f"scored all: {scored['total']} trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
