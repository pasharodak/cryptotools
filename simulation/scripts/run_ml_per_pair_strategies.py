#!/usr/bin/env python3
"""Train ML gates per pair × strategy (April cut), compare to prod by_scenario models.

Uses existing trade_cache from ml_param_experiments (no re-collect).
Loop order: for each pair → all top-31 strategies → train pack winner_exp on that
pair only → score OOS test → also score the same test slice with the prod model.

Outputs:
  simulation/results/ml_per_pair_strategies/report.json
  simulation/results/ml_per_pair_strategies/by_pair/{pair_safe}.json
  simulation/results/ml_per_pair_strategies/comparison_vs_prod.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SIM_SKIP_PERSIST", "1")

import joblib  # noqa: E402

from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    feature_columns,
    make_market_store,
)
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    all_experiments,
    build_split_frames,
    cache_path,
    load_cached,
    run_experiment,
    timerange_to_ms,
)
from simulation.scripts.run_scalp_strategies_compare import (  # noqa: E402
    apply_ml_gate,
    summarize,
)

PACK_PATH = ROOT / "simulation/config/prod_top30_pack.json"
DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache"
DEFAULT_OUT = ROOT / "simulation/results/ml_per_pair_strategies"
PROD_MODEL_DIRS = [
    ROOT / "site/user_data/models/pnl_classifier/by_scenario",
    ROOT / "simulation/results/trade_db/models/by_scenario",
]


def pair_key(pair: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", pair)


def rec_pair(rec: dict[str, Any]) -> str:
    return str(rec.get("pair") or (rec.get("basis") or {}).get("pair") or "")


def load_pack() -> dict[str, Any]:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


def experiments_by_id() -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in all_experiments()}


def resolve_prod_model_dir(sid: str) -> Path | None:
    for base in PROD_MODEL_DIRS:
        d = base / sid
        if (d / "pnl_classifier.joblib").is_file():
            return d
    return None


def load_prod_pipe(sid: str) -> tuple[Any, dict[str, Any]] | None:
    d = resolve_prod_model_dir(sid)
    if d is None:
        return None
    pipe = joblib.load(d / "pnl_classifier.joblib")
    meta: dict[str, Any] = {}
    meta_path = d / "pnl_classifier_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return pipe, meta


def index_cache(
    scenarios: list[str], cache_dir: Path
) -> tuple[dict[str, dict[str, list]], dict[str, dict[str, list]], list[str]]:
    """pair -> sid -> trades for train/test; sorted pair list."""
    train_idx: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    test_idx: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for sid in scenarios:
        tr = load_cached(cache_path(cache_dir, sid, "train")) or []
        te = load_cached(cache_path(cache_dir, sid, "test")) or []
        for rec in tr:
            p = rec_pair(rec)
            if p:
                train_idx[p][sid].append(rec)
        for rec in te:
            p = rec_pair(rec)
            if p:
                test_idx[p][sid].append(rec)
        print(f"indexed {sid}: train={len(tr)} test={len(te)}", flush=True)
    pairs = sorted(set(train_idx) | set(test_idx))
    return train_idx, test_idx, pairs


def score_fitted_pipe(
    pipe: Any,
    test_tr: list[dict[str, Any]],
    test_df: Any,
    *,
    min_profit_proba: float,
) -> dict[str, Any]:
    kept, gate = apply_ml_gate(
        pipe, test_df, test_tr, min_profit_proba=float(min_profit_proba)
    )
    return {
        "min_profit_proba": float(min_profit_proba),
        "gate": gate,
        "test_raw": summarize(test_tr),
        "test_ml": summarize(kept),
        "score": summarize(kept)["pnl"],
    }


def train_pair_scenario(
    *,
    sid: str,
    pair: str,
    train_tr: list[dict[str, Any]],
    test_tr: list[dict[str, Any]],
    spec: dict[str, Any],
    store: Any,
    cut_ms: int,
    min_train: int,
    min_test: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario_id": sid,
        "pair": pair,
        "exp_id": spec["id"],
        "n_train": len(train_tr),
        "n_test": len(test_tr),
        "skipped": False,
    }
    if len(train_tr) < min_train or len(test_tr) < min_test:
        row["skipped"] = True
        row["reason"] = f"need train>={min_train} test>={min_test}"
        return row

    train_df, test_df = build_split_frames(train_tr, test_tr, store, cut_ms)
    if len(train_df) < min_train or len(test_df) < min_test:
        row["skipped"] = True
        row["reason"] = "dataframe too small after features"
        row["n_train_df"] = len(train_df)
        row["n_test_df"] = len(test_df)
        return row

    # Small per-pair sets often break CalibratedClassifierCV(cv=3).
    try:
        result = run_experiment(spec, train_tr, test_tr, train_df, test_df)
    except Exception as exc:
        # Fallback: same model without calibration.
        try:
            soft = dict(spec)
            soft["calibrate"] = False
            soft["id"] = f"{spec['id']}__nocal"
            result = run_experiment(soft, train_tr, test_tr, train_df, test_df)
            result["fallback"] = "nocal"
            result["fallback_from"] = str(exc)
        except Exception as exc2:
            row["skipped"] = True
            row["error"] = f"{exc} | fallback: {exc2}"
            return row

    if result.get("error"):
        row["skipped"] = True
        row["error"] = result["error"]
        return row

    row.update(
        {
            "exp_used": result.get("id"),
            "auc": (result.get("classifier") or {}).get("roc_auc"),
            "profit_recall": (result.get("classifier") or {}).get("profit_recall"),
            "keep_rate": (result.get("gate") or {}).get("keep_rate"),
            "kept": (result.get("test_ml") or {}).get("n"),
            "raw_pnl": (result.get("test_raw") or {}).get("pnl"),
            "ml_pnl": (result.get("test_ml") or {}).get("pnl"),
            "score_ml_test_pnl": result.get("score"),
            "fallback": result.get("fallback"),
        }
    )

    # Prod model on the same OOS slice (already fitted globally).
    prod = load_prod_pipe(sid)
    if prod is not None:
        prod_pipe, meta = prod
        thr = float(
            meta.get("min_profit_proba")
            or spec.get("min_profit_proba")
            or 0.45
        )
        try:
            feats = feature_columns()
            # Ensure columns exist; build_split_frames already has them.
            prod_score = score_fitted_pipe(
                prod_pipe, test_tr, test_df, min_profit_proba=thr
            )
            row["prod_ml_pnl"] = prod_score["score"]
            row["prod_kept"] = (prod_score.get("test_ml") or {}).get("n")
            row["prod_keep_rate"] = (prod_score.get("gate") or {}).get("keep_rate")
            row["prod_min_profit_proba"] = thr
            row["delta_vs_prod"] = round(
                float(row.get("ml_pnl") or 0) - float(prod_score["score"] or 0), 4
            )
        except Exception as exc:
            row["prod_error"] = str(exc)
    else:
        row["prod_error"] = "model not found"

    return row


def aggregate_comparison(
    pair_rows: list[dict[str, Any]], pack: dict[str, Any]
) -> dict[str, Any]:
    by_sid: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "pairs_ok": 0,
            "pairs_skipped": 0,
            "per_pair_ml_pnl": 0.0,
            "prod_on_same_slices_pnl": 0.0,
            "raw_pnl": 0.0,
            "kept": 0,
            "n_test": 0,
        }
    )
    for r in pair_rows:
        sid = r["scenario_id"]
        b = by_sid[sid]
        if r.get("skipped"):
            b["pairs_skipped"] += 1
            continue
        b["pairs_ok"] += 1
        b["per_pair_ml_pnl"] += float(r.get("ml_pnl") or 0)
        b["prod_on_same_slices_pnl"] += float(r.get("prod_ml_pnl") or 0)
        b["raw_pnl"] += float(r.get("raw_pnl") or 0)
        b["kept"] += int(r.get("kept") or 0)
        b["n_test"] += int(r.get("n_test") or 0)

    pack_by = {s["scenario_id"]: s for s in pack.get("strategies") or []}
    rows = []
    for sid, b in by_sid.items():
        pack_row = pack_by.get(sid) or {}
        rows.append(
            {
                "scenario_id": sid,
                "pack_score_ml_test_pnl": pack_row.get("score_ml_test_pnl"),
                "pack_winner_exp": pack_row.get("winner_exp"),
                "per_pair_sum_ml_pnl": round(b["per_pair_ml_pnl"], 4),
                "prod_model_on_pair_slices_pnl": round(b["prod_on_same_slices_pnl"], 4),
                "raw_sum_pnl": round(b["raw_pnl"], 4),
                "delta_per_pair_minus_prod_slices": round(
                    b["per_pair_ml_pnl"] - b["prod_on_same_slices_pnl"], 4
                ),
                "delta_per_pair_minus_pack": round(
                    b["per_pair_ml_pnl"] - float(pack_row.get("score_ml_test_pnl") or 0),
                    4,
                ),
                "pairs_ok": b["pairs_ok"],
                "pairs_skipped": b["pairs_skipped"],
                "kept": b["kept"],
                "n_test": b["n_test"],
            }
        )
    rows.sort(key=lambda x: float(x["per_pair_sum_ml_pnl"]), reverse=True)
    return {
        "n_scenarios": len(rows),
        "total_per_pair_ml_pnl": round(sum(r["per_pair_sum_ml_pnl"] for r in rows), 4),
        "total_prod_slices_ml_pnl": round(
            sum(r["prod_model_on_pair_slices_pnl"] for r in rows), 4
        ),
        "total_pack_ml_pnl": round(
            sum(float(r["pack_score_ml_test_pnl"] or 0) for r in rows), 4
        ),
        "strategies": rows,
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Per-pair × strategy ML train (April cut)")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--max-pairs", type=int, default=0, help="0 = all pairs")
    ap.add_argument("--min-train", type=int, default=25)
    ap.add_argument("--min-test", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="recompute existing pair reports")
    ap.add_argument(
        "--scenarios",
        default="top30",
        help="top30 | all pack ids | comma list",
    )
    args = ap.parse_args()

    pack = load_pack()
    pack_strats = list(pack.get("strategies") or [])
    if args.scenarios.strip().lower() in {"top30", "pack"}:
        scenarios = [s["scenario_id"] for s in pack_strats]
        winner_map = {s["scenario_id"]: s["winner_exp"] for s in pack_strats}
    else:
        scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
        winner_map = {s["scenario_id"]: s["winner_exp"] for s in pack_strats}

    exp_map = experiments_by_id()
    missing_exp = sorted(
        {winner_map[s] for s in scenarios if s in winner_map} - set(exp_map)
    )
    if missing_exp:
        print(f"ERROR: unknown winner experiments: {missing_exp}", flush=True)
        return 1

    te_start, _ = timerange_to_ms(args.test_range)
    cut_date = datetime.fromtimestamp(te_start / 1000, tz=UTC).strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    by_pair_dir = out_dir / "by_pair"
    by_pair_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)

    missing_cache = [
        sid
        for sid in scenarios
        if not (cache_dir / f"{sid}__train.json").is_file()
        or not (cache_dir / f"{sid}__test.json").is_file()
    ]
    if missing_cache:
        print(f"ERROR: missing trade_cache for {missing_cache}", flush=True)
        return 1

    print(
        f"=== PER-PAIR x STRATEGY · {len(scenarios)} strats · "
        f"train={args.train_range} test={args.test_range} cut={cut_date} ===",
        flush=True,
    )
    train_idx, test_idx, pairs = index_cache(scenarios, cache_dir)
    if args.max_pairs and args.max_pairs > 0:
        pairs = pairs[: args.max_pairs]
    print(f"pairs to process: {len(pairs)}", flush=True)

    store = make_market_store(ROOT)
    all_cells: list[dict[str, Any]] = []
    pair_summaries: list[dict[str, Any]] = []

    for pi, pair in enumerate(pairs, 1):
        safe = pair_key(pair)
        pair_path = by_pair_dir / f"{safe}.json"
        if pair_path.is_file() and not args.force:
            prev = json.loads(pair_path.read_text(encoding="utf-8"))
            cells = list(prev.get("strategies") or [])
            all_cells.extend(cells)
            pair_summaries.append(
                {
                    "pair": pair,
                    "skipped_file": True,
                    "n_ok": sum(1 for c in cells if not c.get("skipped")),
                    "sum_ml_pnl": round(
                        sum(float(c.get("ml_pnl") or 0) for c in cells if not c.get("skipped")),
                        4,
                    ),
                    "sum_prod_ml_pnl": round(
                        sum(
                            float(c.get("prod_ml_pnl") or 0)
                            for c in cells
                            if not c.get("skipped")
                        ),
                        4,
                    ),
                }
            )
            print(f"[{pi}/{len(pairs)}] skip {pair} (report exists)", flush=True)
            continue

        print(f"\n[{pi}/{len(pairs)}] PAIR {pair}", flush=True)
        cells: list[dict[str, Any]] = []
        for sid in scenarios:
            train_tr = train_idx[pair].get(sid) or []
            test_tr = test_idx[pair].get(sid) or []
            exp_id = winner_map.get(sid) or "exp14_lgbm_gate045"
            spec = exp_map[exp_id]
            print(
                f"  · {sid} exp={exp_id} train={len(train_tr)} test={len(test_tr)}",
                flush=True,
            )
            cell = train_pair_scenario(
                sid=sid,
                pair=pair,
                train_tr=train_tr,
                test_tr=test_tr,
                spec=spec,
                store=store,
                cut_ms=te_start,
                min_train=args.min_train,
                min_test=args.min_test,
            )
            cells.append(cell)
            if cell.get("skipped"):
                print(f"    skip: {cell.get('reason') or cell.get('error')}", flush=True)
            else:
                print(
                    f"    ml_pnl={cell.get('ml_pnl')} prod={cell.get('prod_ml_pnl')} "
                    f"Δ={cell.get('delta_vs_prod')} auc={cell.get('auc')}",
                    flush=True,
                )

        ok = [c for c in cells if not c.get("skipped")]
        pair_report = {
            "pair": pair,
            "train_range": args.train_range,
            "test_range": args.test_range,
            "cut_date": cut_date,
            "n_strategies": len(cells),
            "n_ok": len(ok),
            "sum_ml_pnl": round(sum(float(c.get("ml_pnl") or 0) for c in ok), 4),
            "sum_prod_ml_pnl": round(sum(float(c.get("prod_ml_pnl") or 0) for c in ok), 4),
            "sum_raw_pnl": round(sum(float(c.get("raw_pnl") or 0) for c in ok), 4),
            "strategies": cells,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        pair_path.write_text(
            json.dumps(pair_report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        all_cells.extend(cells)
        pair_summaries.append(
            {
                "pair": pair,
                "n_ok": pair_report["n_ok"],
                "sum_ml_pnl": pair_report["sum_ml_pnl"],
                "sum_prod_ml_pnl": pair_report["sum_prod_ml_pnl"],
                "sum_raw_pnl": pair_report["sum_raw_pnl"],
            }
        )
        # Incremental global report for resume visibility
        comparison = aggregate_comparison(all_cells, pack)
        report = {
            "mode": "per_pair_x_strategy",
            "train_range": args.train_range,
            "test_range": args.test_range,
            "cut_date": cut_date,
            "n_pairs_done": pi,
            "n_pairs_total": len(pairs),
            "n_scenarios": len(scenarios),
            "min_train": args.min_train,
            "min_test": args.min_test,
            "note": (
                "Each cell trains pack winner_exp on that pair only; "
                "prod_* scores use live by_scenario model on the same OOS slice."
            ),
            "pairs": pair_summaries,
            "comparison_vs_prod": comparison,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        out_dir.joinpath("report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        out_dir.joinpath("comparison_vs_prod.json").write_text(
            json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    comparison = aggregate_comparison(all_cells, pack)
    pair_ranked = sorted(
        pair_summaries, key=lambda r: float(r.get("sum_ml_pnl") or 0), reverse=True
    )
    report = {
        "mode": "per_pair_x_strategy",
        "train_range": args.train_range,
        "test_range": args.test_range,
        "cut_date": cut_date,
        "n_pairs_done": len(pairs),
        "n_pairs_total": len(pairs),
        "n_scenarios": len(scenarios),
        "min_train": args.min_train,
        "min_test": args.min_test,
        "pairs": pair_ranked,
        "top_pairs": pair_ranked[:25],
        "bottom_pairs": list(reversed(pair_ranked[-25:])),
        "comparison_vs_prod": comparison,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    out_dir.joinpath("report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    out_dir.joinpath("comparison_vs_prod.json").write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n========== COMPARISON vs PROD ==========", flush=True)
    print(
        f"sum per-pair models ML PnL: {comparison['total_per_pair_ml_pnl']}",
        flush=True,
    )
    print(
        f"sum prod models on same slices: {comparison['total_prod_slices_ml_pnl']}",
        flush=True,
    )
    print(f"sum pack global scores:       {comparison['total_pack_ml_pnl']}", flush=True)
    print("\nTop strategies by per-pair sum:", flush=True)
    for i, r in enumerate(comparison["strategies"][:15], 1):
        print(
            f"{i:2d}. {r['scenario_id']:22s} "
            f"per_pair={r['per_pair_sum_ml_pnl']:+8.2f} "
            f"prod_slices={r['prod_model_on_pair_slices_pnl']:+8.2f} "
            f"pack={r['pack_score_ml_test_pnl']}",
            flush=True,
        )
    print(f"\nSaved: {out_dir / 'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
