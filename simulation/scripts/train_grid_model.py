#!/usr/bin/env python3
"""Train dedicated live_grid ML model and deploy to user_data."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.per_strategy_models import (  # noqa: E402
    scenario_meta_path,
    scenario_model_dir,
    scenario_model_path,
)
from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    evaluate_pipeline,
    export_record_to_train_rec,
    feature_columns,
    load_trades,
    load_trades_from_export_dir,
    make_market_store,
    make_pipeline,
    time_split,
    trade_dedup_key,
)

GRID_ID = "live_grid"
GRID_ALIASES = {GRID_ID, "live_grid_safe"}
STUDY_EXPORT = ROOT / "simulation/results/full_ml_study/export"
GRID_DATASET_DIR = ROOT / "simulation/results/grid_ml"
LIVE_GRID_MODEL = ROOT / "user_data/models/pnl_classifier/by_scenario/live_grid"


def _normalize_grid_trade(t: dict[str, Any]) -> dict[str, Any]:
    """Map live_grid_safe -> live_grid for unified grid model."""
    out = dict(t)
    sid = out.get("scenario_id") or (out.get("basis") or {}).get("scenario_id")
    if sid in GRID_ALIASES and sid != GRID_ID:
        out["scenario_id"] = GRID_ID
        basis = dict(out.get("basis") or {})
        basis["scenario_id"] = GRID_ID
        basis["grid_train_alias"] = sid
        out["basis"] = basis
    return out


def load_grid_training_trades(root: Path) -> list[dict[str, Any]]:
    merged: dict[tuple, dict] = {}
    db_root = root / "simulation/results/trade_db"
    for t in load_trades(db_root):
        sid = (t.get("scenario_id") or "")
        if sid in GRID_ALIASES:
            merged[trade_dedup_key(t)] = _normalize_grid_trade(t)
    extra_dir = GRID_DATASET_DIR / "export"
    if extra_dir.is_dir():
        for t in load_trades_from_export_dir(extra_dir):
            if t.get("scenario_id") == GRID_ID or (t.get("basis") or {}).get("scenario_id") == GRID_ID:
                merged[trade_dedup_key(t)] = t
    if STUDY_EXPORT.is_dir():
        for t in load_trades_from_export_dir(STUDY_EXPORT):
            if t.get("scenario_id") == GRID_ID:
                merged[trade_dedup_key(t)] = export_record_to_train_rec(t) if "basis" not in t else t
    return list(merged.values())


def save_dataset_jsonl(trades: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")


def train_grid_model(root: Path, *, model_name: str = "lightgbm", min_trades: int = 30) -> dict[str, Any]:
    trades = load_grid_training_trades(root)
    wins = sum(1 for t in trades if float(t.get("profit_abs") or 0) >= 0)
    losses = len(trades) - wins
    print(f"Grid dataset: {len(trades)} trades · W{wins} L{losses}")

    dataset_path = GRID_DATASET_DIR / "dataset.jsonl"
    save_dataset_jsonl(trades, dataset_path)
    stats = {
        "collected_at": datetime.now(tz=UTC).isoformat(),
        "n_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades), 4) if trades else 0,
        "paths": {"dataset": str(dataset_path)},
    }
    (GRID_DATASET_DIR / "dataset_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if len(trades) < min_trades:
        raise RuntimeError(f"not enough grid trades ({len(trades)} < {min_trades})")

    market_store = make_market_store(root)
    df = build_dataframe(trades, market_store)
    train_df, test_df = time_split(df)
    calibrate = len(df) >= 120
    pipe = make_pipeline(model_name, calibrate=calibrate)
    metrics = evaluate_pipeline(pipe, train_df, test_df)

    meta = {
        "scenario_id": GRID_ID,
        "model": f"{model_name} + {'isotonic' if calibrate else 'raw'}",
        "model_key": model_name,
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "train_source": "grid_ml merged (trade_db live_grid+safe + grid export + study)",
        "n_trades": len(df),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "market_coverage": market_store.coverage(df),
        "calibrated": calibrate,
        **metrics,
        "features": feature_columns(),
    }

    sim_dir = scenario_model_dir(root, GRID_ID)
    sim_dir.mkdir(parents=True, exist_ok=True)
    import joblib

    joblib.dump(pipe, scenario_model_path(root, GRID_ID))
    scenario_meta_path(root, GRID_ID).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    LIVE_GRID_MODEL.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, LIVE_GRID_MODEL / "pnl_classifier.joblib")
    (LIVE_GRID_MODEL / "pnl_classifier_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"Trained grid model · acc {meta['accuracy']:.1%} · "
        f"ROC {meta.get('roc_auc')} · profit_recall {meta.get('profit_recall')}"
    )
    print(f"  sim: {scenario_model_path(root, GRID_ID)}")
    print(f"  live: {LIVE_GRID_MODEL / 'pnl_classifier.joblib'}")
    return meta


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Train live_grid dedicated ML model")
    ap.add_argument("--model", default="lightgbm")
    ap.add_argument("--min-trades", type=int, default=30)
    args = ap.parse_args()
    train_grid_model(ROOT, model_name=args.model, min_trades=args.min_trades)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
