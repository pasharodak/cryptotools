#!/usr/bin/env python3
"""Train deployable wide+XGB models for the UI test-strategy block.

Uses the same April cut and trade caches as the blend experiments, but exports
standard pnl_classifier.joblib artifacts the live gate can load.

Source scenarios → test scenario dirs / strategy class names:
  new_psar          → new_psar_test          / PsaraFlipTestStrategy
  chart3_atrch      → chart3_atrch_test      / AtrChannelBreakoutTestStrategy
  trend_breakout    → trend_breakout_test    / AdxMomentumTestStrategy
  trend_supertrend  → trend_supertrend_test  / SupertrendTestStrategy
  chart2_cmf        → chart2_cmf_test        / CmfZeroCrossTestStrategy
"""
from __future__ import annotations

import json
import shutil
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
from xgboost import XGBClassifier

from simulation.ml.market_features import MARKET_FEATURES
from simulation.ml.pnl_classifier import (
    activate_feature_set,
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
)
from simulation.scripts.run_legacy_april_cut_ml import GATES, gate_stats
from simulation.scripts.run_ml_param_experiments import make_exp_pipeline, timerange_to_ms
from simulation.scripts.run_ts_family_3scen_ml import load_trades

TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"
OUT_SIM = ROOT / "simulation/models/pnl_classifier/by_scenario"
OUT_SITE = ROOT / "site/user_data/models/pnl_classifier/by_scenario"
REPORT_DIR = ROOT / "simulation/results/test_block_3scen_ml"

SPECS = [
    {
        "source_scenario": "new_psar",
        "source": "cache",
        "scenario_id": "new_psar_test",
        "class_name": "PsaraFlipTestStrategy",
        "label": "Parabolic SAR flip (test wide)",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "chart3_atrch",
        "source": "cache",
        "scenario_id": "chart3_atrch_test",
        "class_name": "AtrChannelBreakoutTestStrategy",
        "label": "ATR channel breakout (test wide)",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "trend_breakout",
        "source": "trade_db",
        "scenario_id": "trend_breakout_test",
        "class_name": "AdxMomentumTestStrategy",
        "label": "Breakout-Retest (test wide)",
        "default_gate": 0.70,
    },
    {
        "source_scenario": "trend_supertrend",
        "source": "trade_db",
        "scenario_id": "trend_supertrend_test",
        "class_name": "SupertrendTestStrategy",
        "label": "Supertrend (ATR) (test wide)",
        "default_gate": 0.70,
    },
    {
        "source_scenario": "chart2_cmf",
        "source": "cache",
        "scenario_id": "chart2_cmf_test",
        "class_name": "CmfZeroCrossTestStrategy",
        "label": "CMF zero cross (test wide)",
        "default_gate": 0.55,
    },
    {
        "source_scenario": "scalp_ema",
        "source": "cache",
        "scenario_id": "scalp_ema_test",
        "class_name": "ScalpEmaCrossTestStrategy",
        "label": "Scalp EMA 8/21 (test) wide",
        "default_gate": 0.55,
    },
    {
        "source_scenario": "chart3_adosc",
        "source": "cache",
        "scenario_id": "chart3_adosc_test",
        "class_name": "ChaikinOscTestStrategy",
        "label": "Chaikin Oscillator (test) wide",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "new_donchian",
        "source": "cache",
        "scenario_id": "new_donchian_test",
        "class_name": "DonchianBreakoutTestStrategy",
        "label": "Donchian / Turtle (test) wide",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "chart3_ppo",
        "source": "cache",
        "scenario_id": "chart3_ppo_test",
        "class_name": "PpoSignalTestStrategy",
        "label": "PPO signal cross (test) wide",
        "default_gate": 0.55,
    },
    {
        "source_scenario": "combo_don_adx_vol",
        "source": "cache",
        "scenario_id": "combo_don_adx_vol_test",
        "class_name": "DonchianAdxVolComboTestStrategy",
        "label": "Donchian+ADX+Vol (test) wide",
        "default_gate": 0.65,
    },
    {
        "source_scenario": "chart2_obv",
        "source": "cache",
        "scenario_id": "chart2_obv_test",
        "class_name": "ObvEmaCrossTestStrategy",
        "label": "OBV EMA cross (test) wide",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "chart3_elder",
        "source": "cache",
        "scenario_id": "chart3_elder_test",
        "class_name": "ElderRayTestStrategy",
        "label": "Elder Ray Bull/Bear (test) wide",
        "default_gate": 0.45,
    },
    {
        "source_scenario": "scalp_liq_breakout",
        "source": "cache",
        "scenario_id": "scalp_liq_breakout_test",
        "class_name": "AltVolumeBreakoutTestStrategy",
        "label": "Alt volume breakout (test) wide",
        "default_gate": 0.55,
    },
    {
        "source_scenario": "lite_mean_rev",
        "source": "trade_db",
        "scenario_id": "lite_mean_rev_test",
        "class_name": "BollingerRsiTestStrategy",
        "label": "Mean-reversion (BB) (test) wide",
        "default_gate": 0.7,
    },
    {
        "source_scenario": "trend_macd_ema",
        "source": "trade_db",
        "scenario_id": "trend_macd_ema_test",
        "class_name": "MacdEmaTestStrategy",
        "label": "MACD + EMA200 (test) wide",
        "default_gate": 0.45,
    },
]

XGB_SPEC = {
    "id": "test_block_xgb_wide",
    "model": "xgboost",
    "calibrate": True,
    "calibrate_method": "sigmoid",
    "min_profit_proba": 0.45,
    "params": {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "max_depth": 8,
        "min_child_weight": 25,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
    },
}


def _retag(trades: list[dict[str, Any]], *, scenario_id: str, class_name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in trades:
        r = dict(rec)
        r["scenario_id"] = scenario_id
        basis = dict(r.get("basis") or {})
        basis["scenario_id"] = scenario_id
        basis["strategy"] = class_name
        r["basis"] = basis
        trade = dict(r.get("trade") or {})
        trade["enter_tag"] = class_name
        r["trade"] = trade
        out.append(r)
    return out


def _xgb_with_scale(y: np.ndarray) -> dict[str, Any]:
    spec = dict(XGB_SPEC)
    params = dict(spec["params"])
    pos = max(int((y == 1).sum()), 1)
    neg = max(int((y == 0).sum()), 1)
    params["scale_pos_weight"] = neg / pos
    spec["params"] = params
    return spec


def train_one(spec: dict[str, Any], store, cut_ms: int) -> dict[str, Any]:
    src = spec["source_scenario"]
    sid = spec["scenario_id"]
    cls = spec["class_name"]
    train_raw, test_raw = load_trades(src, spec.get("source") or "cache")
    train_tr = _retag(train_raw, scenario_id=sid, class_name=cls)
    test_tr = _retag(test_raw, scenario_id=sid, class_name=cls)
    if len(train_tr) < 40 or len(test_tr) < 10:
        return {
            **spec,
            "skipped": True,
            "reason": f"not enough trades train={len(train_tr)} test={len(test_tr)}",
        }

    df = build_dataframe(train_tr + test_tr, store)
    train_df = df[df["open_ms"] < cut_ms].copy()
    test_df = df[df["open_ms"] >= cut_ms].copy()
    if len(train_df) < 40 or len(test_df) < 10:
        return {
            **spec,
            "skipped": True,
            "reason": f"not enough feature rows train={len(train_df)} test={len(test_df)}",
        }

    profit_by_id: dict[Any, float] = {}
    for rec in train_tr + test_tr:
        rid = rec.get("id")
        pnl_v = rec.get("profit_abs")
        if pnl_v is None:
            pnl_v = (rec.get("trade") or {}).get("profit_abs")
        if rid is not None:
            profit_by_id[rid] = float(pnl_v or 0)

    y_tr = train_df["label"].to_numpy()
    pipe_spec = _xgb_with_scale(y_tr)
    # Fit on train only for metrics, then refit on train for live dump.
    pipe_eval = make_exp_pipeline(pipe_spec)
    metrics = evaluate_pipeline(pipe_eval, train_df, test_df)

    # Gate sweep on test (eval pipeline already fitted inside evaluate_pipeline)
    proba = pipe_eval.predict_proba(test_df[feature_columns()])
    classes = list(getattr(pipe_eval.named_steps["clf"], "classes_", [0, 1]))
    pidx = classes.index(1) if 1 in classes else 1
    p_profit = proba[:, pidx]
    pnl = np.array([profit_by_id.get(i, 0.0) for i in test_df["id"].tolist()], dtype=float)
    by_gate = [gate_stats(p_profit, pnl, thr) for thr in GATES]
    best = max(by_gate, key=lambda x: x["pnl"])
    default_thr = float(spec["default_gate"])
    default_row = next((g for g in by_gate if abs(g["min_profit_proba"] - default_thr) < 1e-9), None)
    chosen = default_row if default_row and default_row["n"] > 0 else best

    out_dir = OUT_SIM / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "scenario_id": sid,
        "source_scenario": src,
        "strategy": cls,
        "experiment_id": "test_block_xgb_wide",
        "model": "xgboost + sigmoid",
        "model_key": "xgboost",
        "calibrate": True,
        "calibrate_method": "sigmoid",
        "feature_set": "wide",
        "market_features": list(MARKET_FEATURES),
        "features": feature_columns(),
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "fit_mode": "train_window",
        "n_train": len(train_df),
        "n_test": len(test_df),
        "n_train_trades": len(train_tr),
        "n_test_trades": len(test_tr),
        "min_profit_proba": chosen["min_profit_proba"],
        "best_gate": best,
        "by_gate": by_gate,
        "source": "test_block_3scen_ml",
        **{k: metrics.get(k) for k in (
            "accuracy", "roc_auc", "profit_precision", "profit_recall", "loss_recall", "macro_f1"
        )},
    }
    pipe_live = make_exp_pipeline(pipe_spec)
    pipe_live.fit(train_df[feature_columns()], train_df["label"])
    joblib.dump(pipe_live, out_dir / "pnl_classifier.joblib")
    (out_dir / "pnl_classifier_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    site_dir = OUT_SITE / sid
    site_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / "pnl_classifier.joblib", site_dir / "pnl_classifier.joblib")
    shutil.copy2(out_dir / "pnl_classifier_meta.json", site_dir / "pnl_classifier_meta.json")

    return {
        **spec,
        "skipped": False,
        "model_dir": str(out_dir),
        "site_dir": str(site_dir),
        "classifier": {k: metrics.get(k) for k in (
            "accuracy", "roc_auc", "profit_precision", "profit_recall", "loss_recall", "macro_f1"
        )},
        "test_raw": {
            "n": len(test_df),
            "pnl": round(float(pnl.sum()), 4),
            "wins": int((pnl >= 0).sum()),
        },
        "chosen_gate": chosen,
        "best_gate": best,
        "by_gate": by_gate,
    }


def main() -> int:
    only = {a.strip() for a in sys.argv[1:] if a and not a.startswith("-")}
    specs = [s for s in SPECS if not only or s["scenario_id"] in only or s["source_scenario"] in only]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    activate_feature_set("wide")
    cut_ms = timerange_to_ms(TEST_RANGE)[0]
    store = make_market_store(ROOT)
    rows: list[dict[str, Any]] = []
    print(
        f"=== Test-block wide XGB · train={TRAIN_RANGE} test={TEST_RANGE} · "
        f"feats={len(MARKET_FEATURES)} ===",
        flush=True,
    )
    for i, spec in enumerate(specs, 1):
        print(f"[{i}/{len(specs)}] {spec['scenario_id']} <- {spec['source_scenario']}...", flush=True)
        row = train_one(spec, store, cut_ms)
        rows.append(row)
        if row.get("skipped"):
            print(f"  SKIP {row.get('reason')}", flush=True)
        else:
            b = row["chosen_gate"]
            print(
                f"  auc={row['classifier'].get('roc_auc')} "
                f"raw_pnl={row['test_raw']['pnl']} "
                f"gate={int(b['min_profit_proba']*100)}% ml_pnl={b['pnl']} n={b['n']}",
                flush=True,
            )

    report_path = REPORT_DIR / "report.json"
    if only and report_path.is_file():
        try:
            prev = json.loads(report_path.read_text(encoding="utf-8"))
            prev_rows = [r for r in (prev.get("scenarios") or []) if r.get("scenario_id") not in {x["scenario_id"] for x in rows}]
            rows = prev_rows + rows
        except (OSError, json.JSONDecodeError):
            pass
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "feature_set": "wide",
        "model": "xgboost + sigmoid",
        "note": "GBDT-wide half of blend experiments; LSTM blend not yet in live gate",
        "scenarios": rows,
    }
    (REPORT_DIR / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    activate_feature_set("core")
    print(f"\nReport: {REPORT_DIR / 'report.json'}", flush=True)
    return 0 if all(not r.get("skipped") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
