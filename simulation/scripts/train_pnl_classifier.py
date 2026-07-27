#!/usr/bin/env python3
"""Train profit/loss classifier on trade_db and score all trades with confidence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import score_all, train_model  # noqa: E402


def main() -> int:
    print("=== Train PnL classifier (profit vs loss) ===")
    metrics = train_model(ROOT)
    print(f"trades: {metrics['n_trades']} (train {metrics['n_train']}, test {metrics['n_test']})")
    print(f"model: {metrics['model']}")
    cov = metrics.get("market_coverage", {})
    if cov:
        print(f"market data coverage: {cov.get('with_market', 0)}/{cov.get('total', 0)} ({100 * cov.get('pct', 0):.1f}%)")
    print(f"accuracy (time split test): {metrics['accuracy']:.1%}")
    if metrics.get("roc_auc") is not None:
        print(f"ROC-AUC: {metrics['roc_auc']:.3f}")
    print("confusion (test):", metrics["confusion_matrix"])
    rep = metrics["classification_report"]
    for cls in ("loss", "profit"):
        r = rep.get(cls, {})
        print(f"  {cls}: precision={r.get('precision', 0):.2f} recall={r.get('recall', 0):.2f}")

    print("\n=== Score all trades ===")
    scored = score_all(ROOT)
    print(f"scored: {scored['total']} · accuracy on full set: {scored['accuracy_on_all']:.1%}")
    print(f"predictions: {ROOT / 'simulation/results/trade_db/models/predictions.json'}")
    print(f"model: {ROOT / 'simulation/results/trade_db/models/pnl_classifier.joblib'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
