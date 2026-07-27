#!/usr/bin/env python3
"""Export trades and train per-scenario PnL models for newly added bots only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.per_strategy_models import MIN_TRADES, train_all_scenario_models  # noqa: E402
from simulation.paths import VENV_PYTHON  # noqa: E402
from simulation.scripts.comparison_common import DEFAULT_TIMERANGE, PROD_PAIRS_PATH, period_months  # noqa: E402
from simulation.scripts.run_full_ml_study import (  # noqa: E402
    EXPORT_DIR,
    STUDY_DIR,
    _count_by_strategy_from_export,
    _save_trade_files,
    run_export_month,
)

PY = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)

# New classic strategies + previously disabled lite/trend bots (skip legacy live proxies).
NEW_TRAINING_SCENARIOS = [
    "trend_supertrend",
    "trend_macd_ema",
    "lite_rsi_ema",
    "lite_swing",
    "lite_scalp",
    "lite_hft",
    "lite_arbitrage",
    "lite_position",
    "trend_fib",
    "trend_liquidity",
    "trend_channel",
]


def load_prod200_pairs() -> list[str]:
    path = ROOT / PROD_PAIRS_PATH
    if not path.is_file():
        raise SystemExit(f"prod pairs file missing: {path} — run with --fetch-pairs")
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs = list(data.get("pairs") or [])
    n = int(data.get("number_assets") or len(pairs))
    if len(pairs) != 200:
        print(f"WARN: expected 200 pairs, got {len(pairs)} (number_assets={n})", flush=True)
    return pairs


def run_step(cmd: list[str], desc: str) -> None:
    print(f"\n=== {desc} ===", flush=True)
    env = os.environ.copy()
    if "train" in desc.lower():
        env["ML_USE_GPU"] = "1"
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise SystemExit(f"{desc} failed (exit {r.returncode})")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Export + train ML for new scenario bots (prod200)")
    ap.add_argument("--timerange", default=DEFAULT_TIMERANGE)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--scan-workers", type=int, default=0)
    ap.add_argument("--model", default="lightgbm")
    ap.add_argument("--min-trades-per-strategy", type=int, default=MIN_TRADES)
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--fetch-pairs", action="store_true", help="Refresh prod_pairs_200.json from Bybit")
    ap.add_argument(
        "--scenarios",
        default=",".join(NEW_TRAINING_SCENARIOS),
        help="Comma-separated scenario ids",
    )
    args = ap.parse_args()

    if args.fetch_pairs:
        run_step(
            [str(PY), str(ROOT / "simulation/scripts/fetch_prod_pairs.py"), "-n", "200"],
            "Fetch prod200 pairs",
        )

    pairs = load_prod200_pairs()
    print(f"=== prod200: {len(pairs)} pairs ===", flush=True)

    if not args.skip_download:
        run_step(
            [
                str(PY),
                str(ROOT / "simulation/scripts/download_all_pairs_history.py"),
                "--timerange",
                args.timerange,
                "--pairs-source",
                "prod200",
                "--batch-size",
                "8",
            ],
            f"Download 5m OHLCV prod200 · {args.timerange}",
        )

    enabled = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    start_s, end_s = args.timerange.split("-")
    y0, m0 = int(start_s[:4]), int(start_s[4:6])
    y1, m1 = int(end_s[:4]), int(end_s[4:6])
    months = period_months(y0, m0, y1, m1)

    print(f"=== NEW SCENARIOS ML · {len(enabled)} bots · {len(pairs)} pairs · {args.timerange} ===")
    print(f"  scenarios: {sorted(enabled)}")

    total = 0
    if not args.skip_export:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        for y, m in months:
            label = f"{y}-{m:02d}"
            print(f"  export {label}...", flush=True)
            _, records, archive = run_export_month(
                pairs,
                y,
                m,
                args.workers,
                enabled,
                pool_mode="direct",
                scan_workers=args.scan_workers,
            )
            _save_trade_files(EXPORT_DIR, records)
            total += len(records)
            w = sum(1 for r in records if float(r.get("profit_abs") or 0) >= 0)
            print(f"    {len(records)} trades · W{w} L{len(records) - w} · archive={archive.get('session_id')}", flush=True)

        index = {
            "exported_at": datetime.now(tz=UTC).isoformat(),
            "timerange": args.timerange,
            "pairs": len(pairs),
            "pairs_source": "prod200",
            "bots": sorted(enabled),
            "new_scenarios_export_trades": total,
            "by_strategy": _count_by_strategy_from_export(),
        }
        (EXPORT_DIR / "new_scenarios_index.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nExport done · {total} trades appended · index: {EXPORT_DIR / 'new_scenarios_index.json'}")

    if not args.skip_train:
        print("\n=== TRAIN per-scenario models (prod200) ===")
        meta = train_all_scenario_models(
            ROOT,
            sorted(enabled),
            model_name=args.model,
            export_dir=EXPORT_DIR if EXPORT_DIR.is_dir() else None,
            min_trades=int(args.min_trades_per_strategy),
        )
        out = STUDY_DIR / "new_scenarios_training.json"
        out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  trained: {meta.get('trained')}")
        print(f"  skipped: {meta.get('skipped')}")
        for sid in sorted(enabled):
            r = (meta.get("results") or {}).get(sid) or {}
            if r.get("skipped"):
                print(f"    {sid}: SKIP — {r.get('reason')} ({r.get('n_trades', 0)} trades)")
            else:
                acc = r.get("accuracy")
                auc = r.get("roc_auc")
                acc_s = f"{acc:.1%}" if acc is not None else "n/a"
                auc_s = f"{auc:.3f}" if auc is not None else "n/a"
                print(
                    f"    {sid}: {r.get('n_trades')} trades · acc {acc_s} · AUC {auc_s}",
                    flush=True,
                )
        print(f"\nTraining summary: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
