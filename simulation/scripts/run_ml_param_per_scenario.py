#!/usr/bin/env python3
"""Per-strategy ML param sweep using existing trade_cache.

Does NOT delete trade_cache. Writes:
  simulation/results/ml_param_experiments/per_scenario/{scenario_id}.json
  simulation/results/ml_param_experiments/report_per_scenario.json
  (+ report_per_scenario_batch{N}.json when --batch > 0)
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.scripts.run_ml_param_experiments import (  # noqa: E402
    ALL_SCENARIOS,
    DEFAULT_OUT,
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    all_experiments,
    load_top30_scenarios,
    resolve_experiments,
    run_per_scenario,
    timerange_to_ms,
)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Per-scenario ML experiments from trade_cache")
    ap.add_argument("--train-range", default=DEFAULT_TRAIN)
    ap.add_argument("--test-range", default=DEFAULT_TEST)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--cache-dir",
        default=str(ROOT / "simulation/results/ml_param_experiments/trade_cache"),
    )
    ap.add_argument("--scenarios", default="all", help="all | top30 | comma ids")
    ap.add_argument(
        "--batch",
        type=int,
        default=0,
        help="0=all exps, 1=01-15, 2=16-30, 3=31-50 (new models+preprocess)",
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--no-merge",
        action="store_true",
        help="Do not merge with previous experiment results in per_scenario/*.json",
    )
    args = ap.parse_args()

    scen_arg = args.scenarios.strip().lower()
    if scen_arg == "all":
        scenarios = list(ALL_SCENARIOS)
    elif scen_arg == "top30":
        scenarios = load_top30_scenarios()
    else:
        scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    experiments = resolve_experiments(args.batch)
    te_start, _ = timerange_to_ms(args.test_range)
    cut_date = datetime.fromtimestamp(te_start / 1000, tz=UTC).strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    cache_dir = Path(args.cache_dir)

    missing = [
        sid
        for sid in scenarios
        if not (cache_dir / f"{sid}__train.json").is_file()
        or not (cache_dir / f"{sid}__test.json").is_file()
    ]
    if missing:
        print(f"ERROR: missing cache for {len(missing)} scenarios: {missing[:8]}...", flush=True)
        print("Refusing to delete/rebuild here — restore cache or run collect first.", flush=True)
        return 1

    print(
        f"=== PER-SCENARIO · batch={args.batch} · {len(experiments)} exps · "
        f"{len(scenarios)} strategies · cut={cut_date} · cache={cache_dir} ===",
        flush=True,
    )
    summary = run_per_scenario(
        scenarios=scenarios,
        experiments=experiments,
        cache_dir=cache_dir,
        out_dir=out_dir,
        cut_ms=te_start,
        cut_date=cut_date,
        train_range=args.train_range,
        test_range=args.test_range,
        force=args.force,
        merge_existing=not args.no_merge,
    )
    if args.batch:
        batch_path = out_dir / f"report_per_scenario_batch{args.batch}.json"
        batch_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Also saved: {batch_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
