#!/usr/bin/env python3
"""Train ML trade finder (market scanner model)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.trade_finder import train_model  # noqa: E402


def main() -> int:
    meta = train_model(ROOT)
    print("\n=== trade finder trained ===")
    print(f"  samples: {meta['n_samples']} (train {meta['n_train']} / test {meta['n_test']})")
    print(f"  ROC-AUC: {meta.get('roc_auc')}  accuracy: {meta.get('accuracy')}")
    print(f"  profit precision: {meta.get('profit_precision')}  recall: {meta.get('profit_recall')}")
    print(f"  positive rate: {meta.get('positive_rate')}")
    print(f"  model: {ROOT / 'simulation/results/trade_db/models/trade_finder.joblib'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
