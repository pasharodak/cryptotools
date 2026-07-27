"""Per-scenario PnL classifiers (one model per strategy/grid bot)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.pipeline import Pipeline

from simulation.ml.pnl_classifier import (
    MODEL_DIR,
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    load_model,
    load_trades,
    load_trades_from_export_dir,
    make_market_store,
    make_pipeline,
    time_split,
    trade_dedup_key,
)

BY_SCENARIO_DIR = "by_scenario"
MIN_TRADES = 50


def scenario_model_dir(root: Path, scenario_id: str) -> Path:
    return root / MODEL_DIR / BY_SCENARIO_DIR / scenario_id


def scenario_model_path(root: Path, scenario_id: str) -> Path:
    return scenario_model_dir(root, scenario_id) / "pnl_classifier.joblib"


def scenario_meta_path(root: Path, scenario_id: str) -> Path:
    return scenario_model_dir(root, scenario_id) / "pnl_classifier_meta.json"


def load_trades_for_scenario(
    db_root: Path,
    scenario_id: str,
    *,
    export_dir: Path | None = None,
) -> list[dict[str, Any]]:
    merged: dict[tuple, dict[str, Any]] = {}
    for t in load_trades(db_root):
        if (t.get("scenario_id") or "") == scenario_id:
            merged[trade_dedup_key(t)] = t
    if export_dir and export_dir.is_dir():
        for rec in load_trades_from_export_dir(export_dir):
            sid = rec.get("scenario_id") or (rec.get("basis") or {}).get("scenario_id")
            if sid != scenario_id:
                continue
            merged[trade_dedup_key(rec)] = rec
    return list(merged.values())


def train_model_for_scenario(
    root: Path,
    scenario_id: str,
    *,
    model_name: str = "lightgbm",
    min_trades: int = MIN_TRADES,
    export_dir: Path | None = None,
) -> dict[str, Any] | None:
    db_root = root / "simulation/results/trade_db"
    trades = load_trades_for_scenario(db_root, scenario_id, export_dir=export_dir)
    if len(trades) < min_trades:
        return {
            "scenario_id": scenario_id,
            "skipped": True,
            "reason": f"not enough trades ({len(trades)} < {min_trades})",
            "n_trades": len(trades),
        }

    market_store = make_market_store(root)
    df = build_dataframe(trades, market_store)
    train_df, test_df = time_split(df)
    pipe = make_pipeline(model_name, calibrate=True)
    metrics = evaluate_pipeline(pipe, train_df, test_df)

    out_dir = scenario_model_dir(root, scenario_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "scenario_id": scenario_id,
        "model": f"{model_name} + isotonic",
        "model_key": model_name,
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "n_trades": len(df),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "market_coverage": market_store.coverage(df),
        **metrics,
        "features": feature_columns(),
    }
    joblib.dump(pipe, scenario_model_path(root, scenario_id))
    scenario_meta_path(root, scenario_id).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return meta


def train_all_scenario_models(
    root: Path,
    scenario_ids: list[str],
    *,
    model_name: str = "lightgbm",
    export_dir: Path | None = None,
    min_trades: int = MIN_TRADES,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for sid in scenario_ids:
        print(f"  train {sid}...", flush=True)
        results[sid] = train_model_for_scenario(
            root, sid, model_name=model_name, export_dir=export_dir, min_trades=min_trades
        )
    ok = [sid for sid, r in results.items() if r and not r.get("skipped")]
    summary = {
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "model_name": model_name,
        "scenarios": scenario_ids,
        "trained": ok,
        "skipped": [sid for sid in scenario_ids if sid not in ok],
        "results": results,
    }
    out = root / MODEL_DIR / BY_SCENARIO_DIR / "training_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def load_model_for_scenario(root: Path, scenario_id: str) -> Pipeline:
    path = scenario_model_path(root, scenario_id)
    if path.is_file():
        return joblib.load(path)
    return load_model(root)
