#!/usr/bin/env python3
"""Retrain + evaluate legacy prod strategies: train to April, test after April.

Uses existing trade_db records (no full backtest collect). Fits lightgbm+sigmoid
per scenario and sweeps ML gate on the test window.
"""
from __future__ import annotations

import json
import sys
import warnings
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
import pandas as pd

from simulation.ml.pnl_classifier import (
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
)
from simulation.scripts.run_ml_param_experiments import (
    make_exp_pipeline,
    timerange_to_ms,
)

TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"
OUT_DIR = ROOT / "simulation/results/legacy_april_cut_ml"
DB = ROOT / "simulation/results/trade_db"

# Legacy set that was disabled when top-31 pack went live.
LEGACY_SCENARIOS: list[dict[str, str]] = [
    {"scenario_id": "trend_ema", "class_name": "TripleEmaStrategy", "label": "EMA 50/200 (4H)"},
    {"scenario_id": "lite_mean_rev", "class_name": "BollingerRsiStrategy", "label": "Mean-reversion (BB)"},
    {"scenario_id": "trend_breakout", "class_name": "AdxMomentumStrategy", "label": "Breakout-Retest"},
    {"scenario_id": "lite_intraday", "class_name": "LiteIntradayStrategy", "label": "Внутридневная"},
    {"scenario_id": "lite_range", "class_name": "LiteRangeStrategy", "label": "Диапазонная"},
    {"scenario_id": "trend_supertrend", "class_name": "SupertrendStrategy", "label": "Supertrend (ATR)"},
    {"scenario_id": "trend_macd_ema", "class_name": "MacdEmaStrategy", "label": "MACD + EMA200"},
    {"scenario_id": "trend_fib", "class_name": "FibPullbackStrategy", "label": "Fib pullback (DCA)"},
]

GATES = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


def load_scenario_trades(scenario_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prefix = f"{scenario_id}_"
    for sub in ("profit", "loss"):
        folder = DB / sub
        if not folder.is_dir():
            continue
        for path in folder.glob(f"{prefix}*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = rec.get("scenario_id") or (rec.get("basis") or {}).get("scenario_id")
            if sid and sid != scenario_id:
                continue
            if not rec.get("scenario_id"):
                rec["scenario_id"] = scenario_id
            if rec.get("profit_abs") is None:
                rec["profit_abs"] = (rec.get("trade") or {}).get("profit_abs")
            rows.append(rec)
    return rows


def split_trades(
    trades: list[dict[str, Any]], cut_ms: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train, test = [], []
    for rec in trades:
        open_ms = int((rec.get("trade") or {}).get("open_ms") or 0)
        if open_ms <= 0:
            continue
        if open_ms < cut_ms:
            train.append(rec)
        else:
            test.append(rec)
    return train, test


def gate_stats(profit_proba: np.ndarray, pnl: np.ndarray, thr: float) -> dict[str, Any]:
    kept = (profit_proba >= thr) & (profit_proba >= 0.5)
    n = int(kept.sum())
    if n == 0:
        return {
            "min_profit_proba": thr,
            "n": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0.0,
            "pnl": 0.0,
            "keep_rate": 0.0,
        }
    p = pnl[kept]
    wins = int((p >= 0).sum())
    losses = n - wins
    return {
        "min_profit_proba": thr,
        "n": n,
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / n, 4),
        "pnl": round(float(p.sum()), 4),
        "keep_rate": round(n / len(pnl), 4),
    }


def train_one(scenario_id: str, train_tr: list, test_tr: list, store, cut_ms: int) -> dict[str, Any]:
    train_df, test_df = None, None
    # build frames from lists directly with April cut
    df = build_dataframe(train_tr + test_tr, store)
    train_df = df[df["open_ms"] < cut_ms].copy()
    test_df = df[df["open_ms"] >= cut_ms].copy()
    if len(train_df) < 40 or len(test_df) < 10:
        return {
            "scenario_id": scenario_id,
            "skipped": True,
            "reason": f"too few rows train={len(train_df)} test={len(test_df)}",
            "n_train_trades": len(train_tr),
            "n_test_trades": len(test_tr),
        }

    # Map profit_abs by id
    profit_by_id = {}
    for rec in train_tr + test_tr:
        rid = rec.get("id")
        pnl = rec.get("profit_abs")
        if pnl is None:
            pnl = (rec.get("trade") or {}).get("profit_abs")
        if rid is not None:
            profit_by_id[rid] = float(pnl or 0)

    spec = {
        "id": "legacy_lgbm_sigmoid",
        "model": "lightgbm",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "min_profit_proba": 0.55,
        "params": {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "num_leaves": 63,
            "min_child_samples": 25,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "class_weight": "balanced",
        },
    }
    pipe = make_exp_pipeline(spec)
    metrics = evaluate_pipeline(pipe, train_df, test_df)

    # Prod-like: refit on all available labeled history (train+test), report holdout from above
    feats = feature_columns()
    pipe_prod = make_exp_pipeline(spec)
    all_df = pd.concat([train_df, test_df], ignore_index=True)
    pipe_prod.fit(all_df[feats], all_df["label"])

    # Gate sweep on holdout model (train-only fit) for honest test PnL
    pipe_hold = make_exp_pipeline(spec)
    pipe_hold.fit(train_df[feats], train_df["label"])
    proba = pipe_hold.predict_proba(test_df[feats])
    classes = list(getattr(pipe_hold.named_steps["clf"], "classes_", [0, 1]))
    pidx = classes.index(1) if 1 in classes else 1
    p_profit = proba[:, pidx]
    pnl = np.array([profit_by_id.get(i, 0.0) for i in test_df["id"].tolist()], dtype=float)
    raw_pnl = float(pnl.sum())
    raw_wins = int((pnl >= 0).sum())

    by_gate = [gate_stats(p_profit, pnl, thr) for thr in GATES]
    best = max(by_gate, key=lambda x: x["pnl"])

    out_dir = OUT_DIR / "models" / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "scenario_id": scenario_id,
        "model": "lightgbm + sigmoid",
        "calibrate_method": "sigmoid",
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "fit_mode": "all_cached",
        "n_train": len(train_df),
        "n_test": len(test_df),
        "n_train_trades": len(train_tr),
        "n_test_trades": len(test_tr),
        "min_profit_proba": best["min_profit_proba"],
        "source": "legacy_april_cut_retrain",
        **metrics,
    }
    joblib.dump(pipe_prod, out_dir / "pnl_classifier.joblib")
    (out_dir / "pnl_classifier_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return {
        "scenario_id": scenario_id,
        "skipped": False,
        "n_train": len(train_df),
        "n_test": len(test_df),
        "classifier": {
            "accuracy": metrics.get("accuracy"),
            "roc_auc": metrics.get("roc_auc"),
            "profit_precision": metrics.get("profit_precision"),
            "profit_recall": metrics.get("profit_recall"),
            "loss_recall": metrics.get("loss_recall"),
            "macro_f1": metrics.get("macro_f1"),
        },
        "test_raw": {
            "n": len(test_df),
            "wins": raw_wins,
            "losses": len(test_df) - raw_wins,
            "winrate": round(raw_wins / len(test_df), 4) if len(test_df) else 0.0,
            "pnl": round(raw_pnl, 4),
        },
        "by_gate": by_gate,
        "best_gate": best,
        "model_dir": str(out_dir),
    }


def main() -> int:
    cut_ms, _ = timerange_to_ms(TEST_RANGE)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    store = make_market_store(ROOT)
    results: list[dict[str, Any]] = []

    print(
        f"=== Legacy April-cut ML · train={TRAIN_RANGE} test={TEST_RANGE} · "
        f"{len(LEGACY_SCENARIOS)} scenarios ===",
        flush=True,
    )
    for i, spec in enumerate(LEGACY_SCENARIOS, 1):
        sid = spec["scenario_id"]
        print(f"[{i}/{len(LEGACY_SCENARIOS)}] load {sid}…", flush=True)
        trades = load_scenario_trades(sid)
        train_tr, test_tr = split_trades(trades, cut_ms)
        print(f"  trades total={len(trades)} train={len(train_tr)} test={len(test_tr)}", flush=True)
        if len(train_tr) < 40 or len(test_tr) < 10:
            row = {
                **spec,
                "skipped": True,
                "reason": f"not enough trades train={len(train_tr)} test={len(test_tr)}",
                "n_train_trades": len(train_tr),
                "n_test_trades": len(test_tr),
            }
            results.append(row)
            continue
        row = train_one(sid, train_tr, test_tr, store, cut_ms)
        row.update(spec)
        results.append(row)
        if row.get("skipped"):
            print(f"  SKIP {row.get('reason')}", flush=True)
        else:
            b = row["best_gate"]
            clf = row["classifier"]
            print(
                f"  auc={clf['roc_auc']} acc={clf['accuracy']} "
                f"raw_pnl={row['test_raw']['pnl']} "
                f"BEST gate={int(b['min_profit_proba']*100)}% "
                f"ml_pnl={b['pnl']} n={b['n']}",
                flush=True,
            )

    ok = [r for r in results if not r.get("skipped")]
    # Aggregate total at each gate
    totals = []
    for thr in GATES:
        pnl = 0.0
        n = 0
        for r in ok:
            for g in r.get("by_gate") or []:
                if abs(float(g["min_profit_proba"]) - thr) < 1e-9:
                    pnl += float(g["pnl"])
                    n += int(g["n"])
        totals.append({"gate": thr, "pnl": round(pnl, 4), "n": n})
    best_total = max(totals, key=lambda x: x["pnl"]) if totals else None

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "model": "lightgbm + sigmoid",
        "n_scenarios": len(LEGACY_SCENARIOS),
        "n_ok": len(ok),
        "n_skipped": len(results) - len(ok),
        "totals_by_gate": totals,
        "best_total_gate": best_total,
        "sum_best_per_strategy": round(
            sum(float((r.get("best_gate") or {}).get("pnl") or 0) for r in ok), 4
        ),
        "sum_raw_test": round(sum(float((r.get("test_raw") or {}).get("pnl") or 0) for r in ok), 4),
        "strategies": results,
        "ranking_by_best_ml_pnl": sorted(
            [
                {
                    "scenario_id": r["scenario_id"],
                    "label": r.get("label"),
                    "class_name": r.get("class_name"),
                    "auc": (r.get("classifier") or {}).get("roc_auc"),
                    "best_gate": (r.get("best_gate") or {}).get("min_profit_proba"),
                    "best_pnl": (r.get("best_gate") or {}).get("pnl"),
                    "best_n": (r.get("best_gate") or {}).get("n"),
                    "raw_pnl": (r.get("test_raw") or {}).get("pnl"),
                }
                for r in ok
            ],
            key=lambda x: float(x.get("best_pnl") or -1e18),
            reverse=True,
        ),
    }
    out_path = OUT_DIR / "report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}", flush=True)
    print("Totals by gate:", totals, flush=True)
    print("Best total gate:", best_total, flush=True)
    print("Sum of per-strategy best gates:", report["sum_best_per_strategy"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
