#!/usr/bin/env python3
"""Hybrid trial: XGB/LGBM (ts-rich) + LSTM blend + GARCH vol filter.

Same 3 scenarios / April cut as prior experiments.
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
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from simulation.ml.entry_seq_model import train_seq_classifier
from simulation.ml.market_features import manifest_datadir
from simulation.ml.pnl_classifier import activate_feature_set, build_dataframe, feature_columns
from simulation.ml.ts_entry_features import TS_RICH_FEATURES
from simulation.scripts.run_ml_param_experiments import timerange_to_ms
from simulation.scripts.run_scalp_strategies_compare import summarize
from simulation.scripts.run_ts_family_3scen_ml import (
    GATES,
    SCENARIOS,
    SEQ_WINDOW,
    TsFeatureStore,
    build_seq_matrix,
    enrich_ts,
    eval_proba,
    load_trades,
    profit_by_id,
)

OUT_DIR = ROOT / "simulation/results/hybrid_3scen_ml"
TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"

BLEND_WEIGHTS = (0.3, 0.5, 0.7)  # weight on GBDT vs LSTM
VOL_CAPS = (0.9, 1.0, 1.2, 1.5, 2.0, 99.0)  # 99 = no filter


def fit_lgbm(train_x: pd.DataFrame, y: np.ndarray, cols: list[str]) -> Pipeline:
    base = LGBMClassifier(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=8,
        num_leaves=63,
        min_child_samples=25,
        subsample=0.8,
        colsample_bytree=0.8,
        class_weight="balanced",
        random_state=42,
        verbose=-1,
        n_jobs=-1,
    )
    pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("clf", CalibratedClassifierCV(base, cv=3, method="sigmoid")),
        ]
    )
    pipe.fit(train_x[cols], y)
    return pipe


def fit_xgb(train_x: pd.DataFrame, y: np.ndarray, cols: list[str]) -> Pipeline:
    # scale_pos_weight from class balance
    pos = max(int((y == 1).sum()), 1)
    neg = max(int((y == 0).sum()), 1)
    base = XGBClassifier(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=8,
        min_child_weight=25,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=neg / pos,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )
    pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("clf", CalibratedClassifierCV(base, cv=3, method="sigmoid")),
        ]
    )
    pipe.fit(train_x[cols], y)
    return pipe


def predict_proba_pipe(pipe: Pipeline, x: pd.DataFrame, cols: list[str]) -> np.ndarray:
    proba = pipe.predict_proba(x[cols])
    clf = pipe.named_steps["clf"]
    classes = list(getattr(clf, "classes_", [0, 1]))
    pidx = classes.index(1) if 1 in classes else 1
    return proba[:, pidx]


def gate_with_vol(
    proba: np.ndarray,
    pnl: np.ndarray,
    vol_z: np.ndarray,
    *,
    thr: float,
    vol_cap: float,
) -> dict[str, Any]:
    kept = (proba >= thr) & (proba >= 0.5) & np.isfinite(vol_z) & (vol_z <= vol_cap)
    # if vol nan, treat as pass when cap is huge
    if vol_cap >= 50:
        kept = (proba >= thr) & (proba >= 0.5)
    else:
        vol_ok = np.where(np.isfinite(vol_z), vol_z <= vol_cap, True)
        kept = (proba >= thr) & (proba >= 0.5) & vol_ok
    n = int(kept.sum())
    if n == 0:
        return {
            "min_profit_proba": thr,
            "vol_cap": vol_cap,
            "n": 0,
            "pnl": 0.0,
            "winrate": 0.0,
            "keep_rate": 0.0,
        }
    p = pnl[kept]
    wins = int((p >= 0).sum())
    return {
        "min_profit_proba": thr,
        "vol_cap": vol_cap,
        "n": n,
        "pnl": round(float(p.sum()), 4),
        "winrate": round(wins / n, 4),
        "keep_rate": round(n / len(pnl), 4),
    }


def sweep_hybrid(
    name: str,
    proba: np.ndarray,
    y: np.ndarray,
    pnl: np.ndarray,
    vol_z: np.ndarray,
    thr_default: float,
) -> dict[str, Any]:
    # fixed gate at thr_default, no vol filter
    base = eval_proba(name, proba, y, pnl, thr_default)
    # joint sweep thr x vol_cap
    grid = []
    for thr in GATES:
        for cap in VOL_CAPS:
            grid.append(gate_with_vol(proba, pnl, vol_z, thr=thr, vol_cap=cap))
    best = max(grid, key=lambda x: x["pnl"])
    base["hybrid_sweep"] = grid
    base["best_hybrid"] = best
    return base


def run_scenario(sc: dict[str, str], store: TsFeatureStore, cut_ms: int) -> dict[str, Any]:
    sid = sc["scenario_id"]
    label = sc["label"]
    print(f"\n======== {label} ({sid}) ========", flush=True)

    activate_feature_set("wide")
    train_tr, test_tr = load_trades(sid, sc["source"])
    mkt = store.mkt
    mkt._frames.clear()

    train_df = build_dataframe(train_tr, mkt)
    test_df = build_dataframe(test_tr, mkt)
    train_df = train_df[train_df["open_ms"] < cut_ms].copy()
    test_df = test_df[test_df["open_ms"] >= cut_ms].copy()
    print(f"frames train={len(train_df)} test={len(test_df)}", flush=True)

    pairs = sorted(set(train_df["pair"].astype(str)) | set(test_df["pair"].astype(str)))
    for i, p in enumerate(pairs, 1):
        store.ts_frame(p)
        if i % 50 == 0 or i == len(pairs):
            print(f"  ts frames {i}/{len(pairs)}", flush=True)

    train_df = enrich_ts(train_df, store, TS_RICH_FEATURES)
    test_df = enrich_ts(test_df, store, TS_RICH_FEATURES)

    y_tr = train_df["label"].to_numpy()
    y_te = test_df["label"].to_numpy()
    pmap = profit_by_id(train_tr + test_tr)
    pnl_te = np.array([pmap.get(i, 0.0) for i in test_df["id"].tolist()], dtype=float)
    vol_z = test_df["garch_ewma_vol_z"].to_numpy(dtype=float)
    raw = summarize(test_tr)

    base_cols = [c for c in feature_columns() if c in train_df.columns]
    num_base = [
        c
        for c in base_cols
        if c
        not in (
            "scenario_id",
            "pair_base",
            "scan_type",
            "group",
            "strategy",
            "is_short",
            "label",
        )
    ]
    rich_cols = num_base + [c for c in TS_RICH_FEATURES if c in train_df.columns]

    thr_default = 0.70 if sid == "trend_breakout" else 0.45
    results: list[dict[str, Any]] = []

    print("  -> lgbm_ts_rich", flush=True)
    lgbm = fit_lgbm(train_df, y_tr, rich_cols)
    p_lgbm = predict_proba_pipe(lgbm, test_df, rich_cols)
    results.append(sweep_hybrid("lgbm_ts_rich", p_lgbm, y_te, pnl_te, vol_z, thr_default))

    print("  -> xgb_ts_rich", flush=True)
    xgb = fit_xgb(train_df, y_tr, rich_cols)
    p_xgb = predict_proba_pipe(xgb, test_df, rich_cols)
    results.append(sweep_hybrid("xgb_ts_rich", p_xgb, y_te, pnl_te, vol_z, thr_default))

    print("  -> seq_lstm", flush=True)
    X_tr = build_seq_matrix(train_df, store)
    X_te = build_seq_matrix(test_df, store)
    lstm = train_seq_classifier(X_tr, y_tr, X_te, y_te, kind="lstm", epochs=8, batch=256)
    p_lstm = lstm.proba_test
    row = sweep_hybrid("seq_lstm", p_lstm, y_te, pnl_te, vol_z, thr_default)
    row["classifier"] = {**row["classifier"], **lstm.metrics}
    results.append(row)

    # Blends
    for w in BLEND_WEIGHTS:
        name = f"blend_xgb_lstm_w{w:.1f}"
        print(f"  -> {name}", flush=True)
        proba = w * p_xgb + (1.0 - w) * p_lstm
        results.append(sweep_hybrid(name, proba, y_te, pnl_te, vol_z, thr_default))

    for w in BLEND_WEIGHTS:
        name = f"blend_lgbm_lstm_w{w:.1f}"
        print(f"  -> {name}", flush=True)
        proba = w * p_lgbm + (1.0 - w) * p_lstm
        results.append(sweep_hybrid(name, proba, y_te, pnl_te, vol_z, thr_default))

    # Stack: max confidence agreement
    print("  -> blend_max_xgb_lstm", flush=True)
    proba_max = np.maximum(p_xgb, p_lstm)
    results.append(sweep_hybrid("blend_max_xgb_lstm", proba_max, y_te, pnl_te, vol_z, thr_default))

    print("  -> blend_min_xgb_lstm", flush=True)
    proba_min = np.minimum(p_xgb, p_lstm)
    results.append(sweep_hybrid("blend_min_xgb_lstm", proba_min, y_te, pnl_te, vol_z, thr_default))

    ok = [r for r in results if r.get("score") is not None]
    best_fixed = max(ok, key=lambda r: float(r["score"])) if ok else None
    best_hyb = (
        max(ok, key=lambda r: float((r.get("best_hybrid") or {}).get("pnl") or -1e18)) if ok else None
    )

    for r in results:
        bh = r.get("best_hybrid") or {}
        print(
            f"     {r['id']}: fixed={r.get('score')}  "
            f"best_hyb={bh.get('pnl')} @thr={bh.get('min_profit_proba')} vol<={bh.get('vol_cap')}",
            flush=True,
        )

    return {
        "scenario_id": sid,
        "label": label,
        "n_train_rows": len(train_df),
        "n_test_rows": len(test_df),
        "n_features": len(rich_cols),
        "test_raw": raw,
        "thr_default": thr_default,
        "experiments": results,
        "winner_fixed": (best_fixed or {}).get("id"),
        "winner_fixed_pnl": (best_fixed or {}).get("score"),
        "winner_hybrid": (best_hyb or {}).get("id"),
        "winner_hybrid_pnl": ((best_hyb or {}).get("best_hybrid") or {}).get("pnl"),
        "winner_hybrid_cfg": (best_hyb or {}).get("best_hybrid"),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    store = TsFeatureStore(manifest_datadir(ROOT))

    rows = []
    for sc in SCENARIOS:
        row = run_scenario(sc, store, cut_ms)
        rows.append(row)
        (OUT_DIR / f"{row['scenario_id']}.json").write_text(
            json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "idea": "XGB/LGBM ts-rich + LSTM blend + GARCH ewma vol_z filter",
        "blend_weights": list(BLEND_WEIGHTS),
        "vol_caps": list(VOL_CAPS),
        "scenarios": rows,
        "ranking": [
            {
                "scenario_id": r["scenario_id"],
                "label": r["label"],
                "winner_fixed": r.get("winner_fixed"),
                "pnl_fixed": r.get("winner_fixed_pnl"),
                "winner_hybrid": r.get("winner_hybrid"),
                "pnl_hybrid": r.get("winner_hybrid_pnl"),
                "hybrid_cfg": r.get("winner_hybrid_cfg"),
            }
            for r in rows
        ],
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("\n========== RANKING ==========", flush=True)
    for r in report["ranking"]:
        cfg = r.get("hybrid_cfg") or {}
        line = (
            f"{r['label']}: fixed={r['pnl_fixed']} ({r['winner_fixed']}) | "
            f"hybrid={r['pnl_hybrid']} ({r['winner_hybrid']}) "
            f"thr={cfg.get('min_profit_proba')} vol<={cfg.get('vol_cap')}"
        )
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    print(f"\nReport: {OUT_DIR / 'report.json'}", flush=True)
    activate_feature_set("core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
