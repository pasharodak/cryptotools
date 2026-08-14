#!/usr/bin/env python3
"""Fair head-to-head: XGB+wide vs hybrid blends (same data/cut/gates).

Scenarios: new_psar, chart3_atrch, trend_breakout
Train 20250101-20260331 / Test 20260401-20260625
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
    TsFeatureStore,
    build_seq_matrix,
    enrich_ts,
    eval_proba,
    load_trades,
    profit_by_id,
)

OUT_DIR = ROOT / "simulation/results/wide_vs_blend_3scen_ml"
TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"


def _xgb(y: np.ndarray) -> XGBClassifier:
    pos = max(int((y == 1).sum()), 1)
    neg = max(int((y == 0).sum()), 1)
    return XGBClassifier(
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


def _lgbm() -> LGBMClassifier:
    return LGBMClassifier(
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


def fit_calibrated(base, train_x: pd.DataFrame, y: np.ndarray, cols: list[str]) -> Pipeline:
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


def num_cols(df: pd.DataFrame) -> list[str]:
    base = [c for c in feature_columns() if c in df.columns]
    skip = {
        "scenario_id",
        "pair_base",
        "scan_type",
        "group",
        "strategy",
        "is_short",
        "label",
    }
    return [c for c in base if c not in skip]


def run_scenario(sc: dict[str, str], store: TsFeatureStore, cut_ms: int) -> dict[str, Any]:
    sid = sc["scenario_id"]
    label = sc["label"]
    print(f"\n======== {label} ({sid}) ========", flush=True)

    train_tr, test_tr = load_trades(sid, sc["source"])
    thr = 0.70 if sid == "trend_breakout" else 0.45
    results: list[dict[str, Any]] = []

    # --- A) XGB + wide (no TS-rich) ---
    activate_feature_set("wide")
    mkt = store.mkt
    mkt._frames.clear()
    train_w = build_dataframe(train_tr, mkt)
    test_w = build_dataframe(test_tr, mkt)
    train_w = train_w[train_w["open_ms"] < cut_ms].copy()
    test_w = test_w[test_w["open_ms"] >= cut_ms].copy()
    cols_w = num_cols(train_w)
    y_tr = train_w["label"].to_numpy()
    y_te = test_w["label"].to_numpy()
    pmap = profit_by_id(train_tr + test_tr)
    pnl_te = np.array([pmap.get(i, 0.0) for i in test_w["id"].tolist()], dtype=float)
    raw = summarize(test_tr)

    print(f"  wide frames train={len(train_w)} test={len(test_w)} feats={len(cols_w)}", flush=True)
    print("  -> xgb_wide", flush=True)
    pipe_xw = fit_calibrated(_xgb(y_tr), train_w, y_tr, cols_w)
    p_xgb_wide = predict_proba_pipe(pipe_xw, test_w, cols_w)
    results.append(eval_proba("xgb_wide", p_xgb_wide, y_te, pnl_te, thr))

    print("  -> lgbm_wide", flush=True)
    pipe_lw = fit_calibrated(_lgbm(), train_w, y_tr, cols_w)
    p_lgbm_wide = predict_proba_pipe(pipe_lw, test_w, cols_w)
    results.append(eval_proba("lgbm_wide", p_lgbm_wide, y_te, pnl_te, thr))

    # --- B) ts-rich + LSTM blends (same test ids order as wide frames) ---
    pairs = sorted(set(train_w["pair"].astype(str)) | set(test_w["pair"].astype(str)))
    for i, p in enumerate(pairs, 1):
        store.ts_frame(p)
        if i % 50 == 0 or i == len(pairs):
            print(f"  ts frames {i}/{len(pairs)}", flush=True)

    train_r = enrich_ts(train_w.copy(), store, TS_RICH_FEATURES)
    test_r = enrich_ts(test_w.copy(), store, TS_RICH_FEATURES)
    cols_r = num_cols(train_r) + [c for c in TS_RICH_FEATURES if c in train_r.columns]
    # dedupe preserve order
    seen = set()
    cols_r = [c for c in cols_r if not (c in seen or seen.add(c))]

    print(f"  rich feats={len(cols_r)}", flush=True)
    print("  -> xgb_ts_rich", flush=True)
    pipe_xr = fit_calibrated(_xgb(y_tr), train_r, y_tr, cols_r)
    p_xgb_rich = predict_proba_pipe(pipe_xr, test_r, cols_r)
    results.append(eval_proba("xgb_ts_rich", p_xgb_rich, y_te, pnl_te, thr))

    print("  -> lgbm_ts_rich", flush=True)
    pipe_lr = fit_calibrated(_lgbm(), train_r, y_tr, cols_r)
    p_lgbm_rich = predict_proba_pipe(pipe_lr, test_r, cols_r)
    results.append(eval_proba("lgbm_ts_rich", p_lgbm_rich, y_te, pnl_te, thr))

    print("  -> seq_lstm", flush=True)
    X_tr = build_seq_matrix(train_r, store)
    X_te = build_seq_matrix(test_r, store)
    lstm = train_seq_classifier(X_tr, y_tr, X_te, y_te, kind="lstm", epochs=8, batch=256)
    p_lstm = lstm.proba_test
    row = eval_proba("seq_lstm", p_lstm, y_te, pnl_te, thr)
    row["classifier"] = {**row["classifier"], **lstm.metrics}
    results.append(row)

    blends = [
        ("blend_xgb_wide_lstm_w0.5", 0.5 * p_xgb_wide + 0.5 * p_lstm),
        ("blend_xgb_wide_lstm_w0.7", 0.7 * p_xgb_wide + 0.3 * p_lstm),
        ("blend_xgb_wide_lstm_w0.3", 0.3 * p_xgb_wide + 0.7 * p_lstm),
        ("blend_lgbm_wide_lstm_w0.5", 0.5 * p_lgbm_wide + 0.5 * p_lstm),
        ("blend_xgb_rich_lstm_w0.5", 0.5 * p_xgb_rich + 0.5 * p_lstm),
        ("blend_xgb_rich_lstm_w0.7", 0.7 * p_xgb_rich + 0.3 * p_lstm),
        ("blend_xgb_rich_lstm_w0.3", 0.3 * p_xgb_rich + 0.7 * p_lstm),
        ("blend_lgbm_rich_lstm_w0.5", 0.5 * p_lgbm_rich + 0.5 * p_lstm),
        ("blend_lgbm_rich_lstm_w0.7", 0.7 * p_lgbm_rich + 0.3 * p_lstm),
    ]
    for name, proba in blends:
        print(f"  -> {name}", flush=True)
        results.append(eval_proba(name, proba, y_te, pnl_te, thr))

    ok = [r for r in results if r.get("score") is not None]
    best_fixed = max(ok, key=lambda r: float(r["score"]))
    best_sweep = max(ok, key=lambda r: float((r.get("best_gate") or {}).get("pnl") or -1e18))

    # spotlight compare
    by_id = {r["id"]: r for r in results}
    spotlight = {
        "xgb_wide": by_id.get("xgb_wide"),
        "best_blend": best_fixed,
        "delta_vs_xgb_wide": round(
            float(best_fixed["score"]) - float(by_id["xgb_wide"]["score"]), 4
        )
        if by_id.get("xgb_wide")
        else None,
    }

    for r in results:
        bg = r.get("best_gate") or {}
        print(
            f"     {r['id']}: fixed={r.get('score')} best={bg.get('pnl')}@{bg.get('min_profit_proba')}",
            flush=True,
        )

    return {
        "scenario_id": sid,
        "label": label,
        "thr_default": thr,
        "n_train_rows": len(train_w),
        "n_test_rows": len(test_w),
        "n_feats_wide": len(cols_w),
        "n_feats_rich": len(cols_r),
        "test_raw": raw,
        "experiments": results,
        "spotlight": {
            "xgb_wide_pnl": (spotlight["xgb_wide"] or {}).get("score"),
            "xgb_wide_best_sweep": ((spotlight["xgb_wide"] or {}).get("best_gate") or {}).get(
                "pnl"
            ),
            "winner_fixed": best_fixed.get("id"),
            "winner_fixed_pnl": best_fixed.get("score"),
            "winner_sweep": best_sweep.get("id"),
            "winner_sweep_pnl": (best_sweep.get("best_gate") or {}).get("pnl"),
            "delta_best_minus_xgb_wide": spotlight["delta_vs_xgb_wide"],
        },
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
        "question": "Is blend better than xgb_wide on the same pipeline?",
        "scenarios": rows,
        "ranking": [r["spotlight"] | {"scenario_id": r["scenario_id"], "label": r["label"]} for r in rows],
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("\n========== XGB_WIDE vs BEST ==========", flush=True)
    for r in report["ranking"]:
        line = (
            f"{r['label']}: xgb_wide={r['xgb_wide_pnl']} | "
            f"best={r['winner_fixed_pnl']} ({r['winner_fixed']}) | "
            f"delta={r['delta_best_minus_xgb_wide']} | "
            f"sweep_best={r['winner_sweep_pnl']} ({r['winner_sweep']})"
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
