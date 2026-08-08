#!/usr/bin/env python3
"""Collect + curated ML gate for liquidity scalp scenarios."""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SIM_SKIP_PERSIST", "1")

from simulation.ml.pnl_classifier import make_market_store  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402
from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    all_experiments,
    build_split_frames,
    cache_path,
    collect_all,
    load_cached,
    run_experiments_on_trades,
    timerange_to_ms,
)
from simulation.scripts.run_ml_sltp_grid import CURATED_IDS  # noqa: E402
from simulation.scripts.run_scalp_strategies_compare import summarize  # noqa: E402

SCENARIOS = ["scalp_liq_breakout", "scalp_liq_sweep"]
CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache"
OUT = ROOT / "simulation/results/ml_param_experiments/liq_scalp_ml"


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-collect", action="store_true")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--max-pairs", type=int, default=0)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    pairs = pairs_from_source(ROOT, "all", min_start="2025-01-01")
    pairs = [p for p in pairs if not p.startswith("XRP/")]
    if args.max_pairs > 0:
        pairs = pairs[: args.max_pairs]

    print(
        f"=== LIQ SCALP ML · scenarios={SCENARIOS} · pairs={len(pairs)} · "
        f"train={args.train_range} test={args.test_range} ===",
        flush=True,
    )

    if not args.skip_collect:
        collect_all(
            SCENARIOS,
            pairs,
            train_range=args.train_range,
            test_range=args.test_range,
            cache_dir=CACHE,
        )
    else:
        for sid in SCENARIOS:
            tr = load_cached(cache_path(CACHE, sid, "train")) or []
            te = load_cached(cache_path(CACHE, sid, "test")) or []
            print(f"cache {sid}: train={len(tr)} test={len(te)}", flush=True)

    by_id = {e["id"]: e for e in all_experiments()}
    experiments = [by_id[i] for i in CURATED_IDS]
    te_start, _ = timerange_to_ms(args.test_range)
    store = make_market_store(ROOT)

    rows = []
    for sid in SCENARIOS:
        train_tr = load_cached(cache_path(CACHE, sid, "train")) or []
        test_tr = load_cached(cache_path(CACHE, sid, "test")) or []
        print(f"\n### {sid} train={len(train_tr)} test={len(test_tr)}", flush=True)
        raw = summarize(test_tr)
        if len(train_tr) < 40 or len(test_tr) < 10:
            rows.append({"scenario_id": sid, "error": "too few trades", "test_raw": raw})
            continue
        train_df, test_df = build_split_frames(train_tr, test_tr, store, te_start)
        results = run_experiments_on_trades(experiments, train_tr, test_tr, train_df, test_df)
        ranked = sorted(results, key=lambda r: float(r.get("score") or -1e18), reverse=True)
        best = ranked[0] if ranked else {}
        report = {
            "scenario_id": sid,
            "train_range": args.train_range,
            "test_range": args.test_range,
            "n_train_trades": len(train_tr),
            "n_test_trades": len(test_tr),
            "test_raw": raw,
            "experiments": results,
            "ranking": [
                {
                    "rank": i + 1,
                    "id": r.get("id"),
                    "score_ml_test_pnl": r.get("score"),
                    "kept": (r.get("test_ml") or {}).get("n"),
                    "winrate": (r.get("test_ml") or {}).get("winrate"),
                    "auc": (r.get("classifier") or {}).get("roc_auc"),
                }
                for i, r in enumerate(ranked)
            ],
            "winner": best.get("id"),
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
        (OUT / f"{sid}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ml = best.get("test_ml") or {}
        clf = best.get("classifier") or {}
        gate = best.get("gate") or {}
        row = {
            "scenario_id": sid,
            "winner": best.get("id"),
            "score_ml_test_pnl": best.get("score"),
            "test_ml": ml,
            "test_raw": raw,
            "auc": clf.get("roc_auc"),
            "accuracy": clf.get("accuracy"),
            "keep_rate": gate.get("keep_rate"),
            "n_train": len(train_tr),
            "n_test": len(test_tr),
        }
        rows.append(row)
        print(
            f"  RAW pnl={raw.get('pnl')} WR={raw.get('winrate')} n={raw.get('n')}",
            flush=True,
        )
        print(
            f"  WINNER {row['winner']} ml_pnl={row['score_ml_test_pnl']} "
            f"kept={ml.get('n')} WR={ml.get('winrate')} "
            f"W/L={ml.get('wins')}/{ml.get('losses')} auc={row['auc']}",
            flush=True,
        )

    summary = {
        "updated": datetime.now(tz=UTC).isoformat(),
        "train_range": args.train_range,
        "test_range": args.test_range,
        "experiment_ids": CURATED_IDS,
        "rows": rows,
    }
    (OUT / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSaved {OUT / 'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
