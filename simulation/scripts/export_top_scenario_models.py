#!/usr/bin/env python3
"""Train & export top-N per-scenario models from ML param experiment winners.

Uses trade_cache + report_per_scenario.json. Writes:
  simulation/results/trade_db/models/by_scenario/{sid}/
  site/user_data/models/pnl_classifier/by_scenario/{sid}/
  simulation/config/prod_top30_pack.json  (manifest for wiring)
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    evaluate_pipeline,
    feature_columns,
    make_market_store,
)
from simulation.ml.per_strategy_models import scenario_model_dir  # noqa: E402
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    all_experiments,
    build_split_frames,
    cache_path,
    load_cached,
    make_exp_pipeline,
    timerange_to_ms,
)

DEFAULT_REPORT = ROOT / "simulation/results/ml_param_experiments/report_per_scenario.json"
DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache"
DEFAULT_PER = ROOT / "simulation/results/ml_param_experiments/per_scenario"
SITE_BY = ROOT / "site/user_data/models/pnl_classifier/by_scenario"
MANIFEST = ROOT / "simulation/config/prod_top30_pack.json"
SCENARIOS_JSON = ROOT / "simulation/config/player_scenarios.json"

TOP_N = 31
TRAIN_RANGE = "20250101-20260331"
TEST_RANGE = "20260401-20260625"


def load_scenario_meta() -> dict[str, dict[str, Any]]:
    rows = json.loads(SCENARIOS_JSON.read_text(encoding="utf-8"))
    return {r["id"]: r for r in rows}


def pick_top(report: dict[str, Any], n: int) -> list[dict[str, Any]]:
    rows = [r for r in report.get("scenarios") or [] if r.get("score_ml_test_pnl") is not None]
    rows = sorted(rows, key=lambda r: float(r["score_ml_test_pnl"]), reverse=True)
    return rows[:n]


def winner_spec(sid: str, winner_id: str) -> dict[str, Any]:
    path = DEFAULT_PER / f"{sid}.json"
    detail = json.loads(path.read_text(encoding="utf-8"))
    for e in detail.get("experiments") or []:
        if e.get("id") == winner_id and not e.get("error"):
            spec = dict(e.get("spec") or {})
            # rebuild full experiment dict expected by make_exp_pipeline
            for full in all_experiments():
                if full["id"] == winner_id:
                    return full
            return {
                "id": winner_id,
                "model": spec.get("model", "lightgbm"),
                "calibrate": spec.get("calibrate", True),
                "min_profit_proba": spec.get("min_profit_proba", 0.55),
                "params": spec.get("params") or {},
            }
    raise KeyError(f"winner {winner_id} not found for {sid}")


def train_one(
    sid: str,
    winner_id: str,
    *,
    cut_ms: int,
    store: Any,
    fit_all: bool = True,
    calibrate_method: str | None = None,
) -> dict[str, Any]:
    spec = winner_spec(sid, winner_id)
    if calibrate_method and spec.get("calibrate", True):
        spec = {**spec, "calibrate_method": calibrate_method}
    cal_method = None
    if spec.get("calibrate", True):
        cal_method = str(spec.get("calibrate_method") or "isotonic")
    train_tr = load_cached(cache_path(DEFAULT_CACHE, sid, "train")) or []
    test_tr = load_cached(cache_path(DEFAULT_CACHE, sid, "test")) or []
    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
    pipe = make_exp_pipeline(spec)
    if fit_all:
        # Prod: fit on all labeled history available in cache (train+test windows).
        import pandas as pd

        all_df = pd.concat([train_df, test_df], ignore_index=True)
        feats = feature_columns()
        pipe.fit(all_df[feats], all_df["label"])
        # still report holdout metrics from train→test fit for meta transparency
        pipe_eval = make_exp_pipeline(spec)
        metrics = evaluate_pipeline(pipe_eval, train_df, test_df)
        # replace with production pipe
        fitted = pipe
    else:
        metrics = evaluate_pipeline(pipe, train_df, test_df)
        fitted = pipe

    out_sim = scenario_model_dir(ROOT, sid)
    out_sim.mkdir(parents=True, exist_ok=True)
    out_site = SITE_BY / sid
    out_site.mkdir(parents=True, exist_ok=True)

    model_label = spec["model"]
    if cal_method:
        model_label = f"{spec['model']} + {cal_method}"
    meta = {
        "scenario_id": sid,
        "experiment_id": winner_id,
        "model": model_label,
        "model_key": spec["model"],
        "calibrate": bool(spec.get("calibrate", True)),
        "calibrate_method": cal_method,
        "min_profit_proba": float(spec["min_profit_proba"]),
        "params": spec.get("params") or {},
        "trained_at": datetime.now(tz=UTC).isoformat(),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "fit_mode": "all_cached" if fit_all else "train_only",
        "n_train_trades": len(train_tr),
        "n_test_trades": len(test_tr),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "features": feature_columns(),
        "source": "ml_param_experiments_per_scenario",
        **metrics,
    }
    joblib.dump(fitted, out_sim / "pnl_classifier.joblib")
    (out_sim / "pnl_classifier_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copy2(out_sim / "pnl_classifier.joblib", out_site / "pnl_classifier.joblib")
    shutil.copy2(out_sim / "pnl_classifier_meta.json", out_site / "pnl_classifier_meta.json")
    return meta


def build_manifest(top_rows: list[dict[str, Any]], metas: dict[str, Any]) -> dict[str, Any]:
    sc_meta = load_scenario_meta()
    strategies: list[dict[str, Any]] = []
    for i, row in enumerate(top_rows, 1):
        sid = row["scenario_id"]
        sc = sc_meta[sid]
        cls = sc["strategy"]
        sl = float(sc.get("stoploss") or -0.02)
        roi = sc.get("minimal_roi") or {"0": 0.014}
        tp = float(roi.get("0") or roi.get(0) or 0.014)
        m = metas.get(sid) or {}
        strategies.append(
            {
                "rank": i,
                "scenario_id": sid,
                "class_name": cls,
                "label": sc.get("label") or sid,
                "group": sc.get("group") or "ta",
                "stoploss": sl,
                "tp": tp,
                "winner_exp": row.get("winner"),
                "score_ml_test_pnl": row.get("score_ml_test_pnl"),
                "min_profit_proba": m.get("min_profit_proba"),
                "sim_module": {
                    "scalp": "SimScalpingStrategies",
                    "newset": "SimNewSetStrategies",
                    "chart": "SimChartTaStrategies",
                    "chart2": "SimChartTaWave2Strategies",
                    "chart3": "SimChartTaWave3Strategies",
                    "combo": "SimComboStrategies",
                }.get(sc.get("group") or "", ""),
            }
        )
    return {
        "updated": datetime.now(tz=UTC).isoformat(),
        "top_n": len(strategies),
        "train_range": TRAIN_RANGE,
        "test_range": TEST_RANGE,
        "strategies": strategies,
        "legacy_disabled": [
            "CriptoPairsStrategy",
            "SupertrendStrategy",
            "MacdEmaStrategy",
            "FibPullbackStrategy",
            "TripleEmaStrategy",
            "BollingerRsiStrategy",
            "AdxMomentumStrategy",
            "LiteIntradayStrategy",
            "LiteRangeStrategy",
        ],
    }


def load_pack_rows() -> list[dict[str, Any]]:
    """Current prod pack winners (preserves order / ranking already in pack)."""
    pack = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for s in pack.get("strategies") or []:
        sid = s.get("scenario_id")
        wid = s.get("winner_exp")
        if not sid or not wid:
            continue
        rows.append(
            {
                "scenario_id": sid,
                "winner": wid,
                "score_ml_test_pnl": s.get("score_ml_test_pnl"),
            }
        )
    return rows


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--fit-all", action="store_true", default=True)
    ap.add_argument("--train-only-fit", action="store_true")
    ap.add_argument(
        "--from-pack",
        action="store_true",
        help="Retrain winners from prod_top30_pack.json (do not re-rank report)",
    )
    ap.add_argument(
        "--calibrate-method",
        choices=("isotonic", "sigmoid"),
        default=None,
        help="Override calibration method on winner specs (use sigmoid for smooth confidence %)",
    )
    ap.add_argument(
        "--skip-manifest",
        action="store_true",
        help="Do not rewrite prod_top30_pack.json (only models + meta)",
    )
    args = ap.parse_args()
    fit_all = not args.train_only_fit

    if args.from_pack:
        top = load_pack_rows()
        # Honor --top only when explicitly passed (default TOP_N must not truncate pack).
        if "--top" in sys.argv and args.top < len(top):
            top = top[: args.top]
    else:
        report = json.loads(DEFAULT_REPORT.read_text(encoding="utf-8"))
        top = pick_top(report, args.top)
    te_start, _ = timerange_to_ms(TEST_RANGE)
    store = make_market_store(ROOT)
    metas: dict[str, Any] = {}
    cal = args.calibrate_method or "default"
    print(
        f"=== Train {len(top)} winner models (fit_all={fit_all}, calibrate={cal}, "
        f"from_pack={args.from_pack}) ===",
        flush=True,
    )
    for i, row in enumerate(top, 1):
        sid = row["scenario_id"]
        wid = row["winner"]
        print(f"[{i}/{len(top)}] {sid} · {wid} · ml_pnl={row.get('score_ml_test_pnl')}", flush=True)
        metas[sid] = train_one(
            sid,
            wid,
            cut_ms=te_start,
            store=store,
            fit_all=fit_all,
            calibrate_method=args.calibrate_method,
        )
        print(
            f"  auc={metas[sid].get('roc_auc')} gate={metas[sid].get('min_profit_proba')} "
            f"cal={metas[sid].get('calibrate_method')}",
            flush=True,
        )

    if not args.skip_manifest:
        manifest = build_manifest(top, metas)
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nManifest: {MANIFEST}", flush=True)
    else:
        # Keep pack ranking/UI fields; refresh calibrate note on each strategy meta only via models.
        print("\nSkipped rewriting prod_top30_pack.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
