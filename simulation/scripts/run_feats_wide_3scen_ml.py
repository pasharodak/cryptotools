#!/usr/bin/env python3
"""Train several models for 3 prod scenarios with core vs wide market features.

Scenarios:
  #1  new_psar       Parabolic SAR flip
  #2  chart3_atrch   ATR channel breakout
  #32 trend_breakout Breakout-Retest

Train: 20250101-20260331  |  Test: 20260401-20260625
Wide features = longer RSI/ATR/ADX/returns + 24h/48h trend + BB width + trend_4h_*.
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from simulation.ml.pnl_classifier import activate_feature_set, make_market_store
from simulation.scripts.run_legacy_april_cut_ml import load_scenario_trades, split_trades
from simulation.scripts.run_ml_param_experiments import (
    build_split_frames,
    cache_path,
    load_cached,
    make_exp_pipeline,
    timerange_to_ms,
)

TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"
CACHE_DIR = ROOT / "simulation/results/ml_param_experiments/trade_cache"
OUT_DIR = ROOT / "simulation/results/feats_wide_3scen_ml"

SCENARIOS: list[dict[str, str]] = [
    {"scenario_id": "new_psar", "label": "#1 Parabolic SAR flip", "source": "cache"},
    {"scenario_id": "chart3_atrch", "label": "#2 ATR channel breakout", "source": "cache"},
    {"scenario_id": "trend_breakout", "label": "#32 Breakout-Retest", "source": "trade_db"},
]

_LGBM = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "max_depth": 8,
    "num_leaves": 63,
    "min_child_samples": 25,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "class_weight": "balanced",
}

# Models to compare under each feature set.
MODEL_SPECS: list[dict[str, Any]] = [
    {
        "id": "lgbm_gate045",
        "model": "lightgbm",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "default",
        "min_profit_proba": 0.45,
        "params": dict(_LGBM),
    },
    {
        "id": "lgbm_gate055",
        "model": "lightgbm",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "default",
        "min_profit_proba": 0.55,
        "params": dict(_LGBM),
    },
    {
        "id": "lgbm_gate070",
        "model": "lightgbm",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "default",
        "min_profit_proba": 0.70,
        "params": dict(_LGBM),
    },
    {
        "id": "xgb_robust_gate045",
        "model": "xgboost",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "robust",
        "min_profit_proba": 0.45,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "min_child_weight": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
        },
    },
    {
        "id": "mlp_standard_gate055",
        "model": "mlp",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "preprocess": "standard",
        "min_profit_proba": 0.55,
        "params": {
            "hidden_layer_sizes": (64, 32),
            "alpha": 1e-3,
            "learning_rate_init": 0.001,
            "early_stopping": True,
            "validation_fraction": 0.1,
        },
    },
]

FEATURE_SETS = ("core", "wide")
GATES = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


def load_trades(sid: str, source: str) -> tuple[list[dict], list[dict]]:
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    end_ms = timerange_to_ms(TEST_RANGE)[1]
    if source == "cache":
        train = load_cached(cache_path(CACHE_DIR, sid, "train")) or []
        test = load_cached(cache_path(CACHE_DIR, sid, "test")) or []
        if not train or not test:
            raise FileNotFoundError(f"missing trade_cache for {sid} under {CACHE_DIR}")
        return train, test
    # trade_db: split by April cut, clip test end
    rows = load_scenario_trades(sid)
    train, test = split_trades(rows, cut_ms)
    test = [
        r
        for r in test
        if int((r.get("trade") or {}).get("open_ms") or 0) <= end_ms
    ]
    return train, test


def gate_sweep(pipe, test_df, test_tr: list[dict], gates=GATES) -> list[dict[str, Any]]:
    """Evaluate ML PnL across confidence thresholds (same logic as apply_ml_gate)."""
    from simulation.scripts.run_scalp_strategies_compare import apply_ml_gate, summarize

    out = []
    for thr in gates:
        kept, gate = apply_ml_gate(pipe, test_df, test_tr, min_profit_proba=float(thr))
        ml = summarize(kept)
        out.append(
            {
                "min_profit_proba": thr,
                "n": ml["n"],
                "pnl": ml["pnl"],
                "winrate": ml.get("winrate"),
                "keep_rate": gate.get("keep_rate"),
            }
        )
    return out


def run_sid(
    *,
    sid: str,
    label: str,
    source: str,
    feature_set: str,
    store,
    cut_ms: int,
) -> dict[str, Any]:
    from simulation.ml.pnl_classifier import evaluate_pipeline, feature_columns
    from simulation.scripts.run_scalp_strategies_compare import apply_ml_gate, summarize

    activate_feature_set(feature_set)
    # Drop OHLCV indicator cache so enrich uses current MARKET_FEATURES selection.
    store._frames.clear()

    train_tr, test_tr = load_trades(sid, source)
    print(
        f"\n=== {label} ({sid}) · features={feature_set} · "
        f"train={len(train_tr)} test={len(test_tr)} ===",
        flush=True,
    )
    if len(train_tr) < 40 or len(test_tr) < 10:
        return {
            "scenario_id": sid,
            "label": label,
            "feature_set": feature_set,
            "skipped": True,
            "reason": "too few trades",
            "n_train": len(train_tr),
            "n_test": len(test_tr),
        }

    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
    print(f"  frames train={len(train_df)} test={len(test_df)}", flush=True)
    feats = feature_columns()

    results: list[dict[str, Any]] = []
    for spec in MODEL_SPECS:
        exp_id = f"{feature_set}__{spec['id']}"
        spec_run = {**spec, "id": exp_id}
        print(f"  -> {exp_id}", flush=True)
        try:
            pipe = make_exp_pipeline(spec_run)
            clf_metrics = evaluate_pipeline(pipe, train_df, test_df)
            thr = float(spec_run["min_profit_proba"])
            kept, gate = apply_ml_gate(pipe, test_df, test_tr, min_profit_proba=thr)
            raw = summarize(test_tr)
            ml = summarize(kept)
            sweep = gate_sweep(pipe, test_df, test_tr)
            best = max(sweep, key=lambda x: float(x["pnl"]))
            row = {
                "id": exp_id,
                "base_model_id": spec["id"],
                "feature_set": feature_set,
                "spec": {
                    "model": spec["model"],
                    "preprocess": spec.get("preprocess") or "default",
                    "min_profit_proba": thr,
                    "calibrate_method": spec.get("calibrate_method"),
                },
                "n_train": len(train_tr),
                "n_test": len(test_tr),
                "n_features": len(feats),
                "classifier": clf_metrics,
                "gate": gate,
                "test_raw": raw,
                "test_ml": ml,
                "score": ml["pnl"],
                "gate_sweep": sweep,
                "best_gate": best,
            }
            print(
                f"     auc={clf_metrics.get('roc_auc')} ml_pnl={ml['pnl']} "
                f"best_sweep={best['pnl']}@{best['min_profit_proba']}",
                flush=True,
            )
            results.append(row)
        except Exception as exc:
            results.append(
                {
                    "id": exp_id,
                    "feature_set": feature_set,
                    "base_model_id": spec["id"],
                    "error": str(exc),
                    "score": None,
                }
            )
            print(f"     ERROR: {exc}", flush=True)

    ok = [r for r in results if r.get("score") is not None]
    winner = max(ok, key=lambda r: float(r["score"])) if ok else None
    best_sweep = (
        max(ok, key=lambda r: float((r.get("best_gate") or {}).get("pnl") or -1e18)) if ok else None
    )
    return {
        "scenario_id": sid,
        "label": label,
        "feature_set": feature_set,
        "skipped": False,
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "n_train_rows": len(train_df),
        "n_test_rows": len(test_df),
        "n_features": len(feats),
        "experiments": results,
        "winner_fixed_gate": (winner or {}).get("id"),
        "winner_fixed_score": (winner or {}).get("score"),
        "winner_best_gate": (best_sweep or {}).get("id"),
        "winner_best_gate_pnl": ((best_sweep or {}).get("best_gate") or {}).get("pnl"),
        "winner_best_gate_thr": ((best_sweep or {}).get("best_gate") or {}).get(
            "min_profit_proba"
        ),
    }


def main() -> int:
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    store = make_market_store(ROOT)

    all_rows: list[dict[str, Any]] = []
    for sc in SCENARIOS:
        for fs in FEATURE_SETS:
            row = run_sid(
                sid=sc["scenario_id"],
                label=sc["label"],
                source=sc["source"],
                feature_set=fs,
                store=store,
                cut_ms=cut_ms,
            )
            all_rows.append(row)
            (OUT_DIR / f"{sc['scenario_id']}__{fs}.json").write_text(
                json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

    # Ranking: per scenario, compare best core vs best wide
    compare: list[dict[str, Any]] = []
    for sc in SCENARIOS:
        sid = sc["scenario_id"]
        by_fs = {r["feature_set"]: r for r in all_rows if r.get("scenario_id") == sid}
        core = by_fs.get("core") or {}
        wide = by_fs.get("wide") or {}
        compare.append(
            {
                "scenario_id": sid,
                "label": sc["label"],
                "core_best_fixed": core.get("winner_fixed_score"),
                "core_best_exp": core.get("winner_fixed_gate"),
                "wide_best_fixed": wide.get("winner_fixed_score"),
                "wide_best_exp": wide.get("winner_fixed_gate"),
                "core_best_sweep_pnl": core.get("winner_best_gate_pnl"),
                "wide_best_sweep_pnl": wide.get("winner_best_gate_pnl"),
                "delta_fixed": (
                    None
                    if core.get("winner_fixed_score") is None
                    or wide.get("winner_fixed_score") is None
                    else round(
                        float(wide["winner_fixed_score"]) - float(core["winner_fixed_score"]),
                        4,
                    )
                ),
                "delta_sweep": (
                    None
                    if core.get("winner_best_gate_pnl") is None
                    or wide.get("winner_best_gate_pnl") is None
                    else round(
                        float(wide["winner_best_gate_pnl"])
                        - float(core["winner_best_gate_pnl"]),
                        4,
                    )
                ),
            }
        )

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "feature_sets": list(FEATURE_SETS),
        "wide_features": [
            "rsi_28",
            "atr_pct_28",
            "adx_28",
            "vol_ratio_48",
            "ret_24",
            "ret_48",
            "trend_24h",
            "trend_48h",
            "ema_spread_slow_pct",
            "dist_high_48_pct",
            "dist_low_48_pct",
            "bb_width_20",
            "trend_4h_*",
        ],
        "models": [s["id"] for s in MODEL_SPECS],
        "compare": compare,
        "scenarios": all_rows,
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("\n========== COMPARE (ML test PnL) ==========", flush=True)
    for c in compare:
        line = (
            f"{c['label']}: core={c['core_best_fixed']} ({c['core_best_exp']})  "
            f"wide={c['wide_best_fixed']} ({c['wide_best_exp']})  "
            f"d_fixed={c['delta_fixed']}  d_sweep={c['delta_sweep']}"
        )
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    print(f"\nReport: {OUT_DIR / 'report.json'}", flush=True)
    # restore core for other processes in same interpreter
    activate_feature_set("core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
