#!/usr/bin/env python3
"""
Prod-year ML pipeline:
  1. (optional) deploy prod ML to VPS
  2. fetch top-200 Bybit USDT futures (prod pairlist)
  3. download 5m OHLCV for full timerange
  4. export all scanner trades (no ML) -> dataset
  5. train global + per-strategy + grid models
  6. compare no ML / global ML / per-strategy ML
  7. write TXT report
"""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = ROOT / ".venv/Scripts/python.exe"
STUDY = ROOT / "simulation/scripts/run_full_ml_study.py"
MODEL_DIR = ROOT / "simulation/results/trade_db/models"
USER_MODEL = ROOT / "user_data/models/pnl_classifier"


def sync_models_to_user_data() -> None:
    """Copy sim-trained models to user_data for next deploy."""
    import shutil

    USER_MODEL.mkdir(parents=True, exist_ok=True)
    for name in ("pnl_classifier.joblib", "pnl_classifier_meta.json"):
        src = MODEL_DIR / name
        if src.is_file():
            shutil.copy2(src, USER_MODEL / name)
    by_src = MODEL_DIR / "by_scenario"
    by_dst = USER_MODEL / "by_scenario"
    if by_src.is_dir():
        by_dst.mkdir(parents=True, exist_ok=True)
        for sub in by_src.iterdir():
            if sub.is_dir():
                dst = by_dst / sub.name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(sub, dst)
    print(f"Synced models -> {USER_MODEL}")


def run(cmd: list[str], *, desc: str) -> None:
    print(f"\n{'='*60}\n=== {desc}\n{'='*60}", flush=True)
    env = os.environ.copy()
    if "train" in desc.lower() or "grid model" in desc.lower():
        env["ML_USE_GPU"] = "1"
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise SystemExit(f"{desc} failed (exit {r.returncode})")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Prod 200 pairs · year ML pipeline")
    ap.add_argument("--deploy", action="store_true", help="Deploy prod ML to VPS first")
    ap.add_argument("--timerange", default="20250101-20260706")
    ap.add_argument("--pairs", default="prod200", choices=("prod200", "all", "pool"))
    ap.add_argument("--workers", type=int, default=1, help="Export workers (1 recommended)")
    ap.add_argument(
        "--scan-workers",
        type=int,
        default=0,
        help="Parallel workers for scanner replay in export/grid (0=auto cpu_count)",
    )
    ap.add_argument("--model", default="lightgbm")
    ap.add_argument("--fresh", action="store_true", help="Clear export and re-run sim")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-compare", action="store_true")
    ap.add_argument("--skip-grid-train", action="store_true")
    ap.add_argument("--skip-report", action="store_true")
    ap.add_argument("--min-trades-per-strategy", type=int, default=50)
    args = ap.parse_args()

    if args.deploy:
        deploy_ps1 = ROOT / "scripts/deploy_prod_ml.ps1"
        if not deploy_ps1.is_file():
            raise SystemExit(f"deploy script not found: {deploy_ps1}")
        run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(deploy_ps1)],
            desc="Deploy prod ML to VPS",
        )

    run([str(PY), str(ROOT / "simulation/scripts/fetch_prod_pairs.py"), "-n", "200"], desc="Fetch prod 200 pairs")

    if not args.skip_download:
        run(
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
            desc=f"Download 5m OHLCV · {args.timerange}",
        )

    if not args.skip_export:
        export_cmd = [
            str(PY),
            str(STUDY),
            "--phase",
            "export",
            "--pairs",
            args.pairs,
            "--timerange",
            args.timerange,
            "--workers",
            str(args.workers),
            "--scan-workers",
            str(args.scan_workers),
        ]
        if args.fresh:
            export_cmd.append("--fresh")
        run(export_cmd, desc=f"Export scanner trades · {args.pairs} · {args.timerange}")

    if not args.skip_train:
        run(
            [
                str(PY),
                str(STUDY),
                "--phase",
                "train",
                "--train-source",
                "all",
                "--model",
                args.model,
                "--min-trades-per-strategy",
                str(args.min_trades_per_strategy),
            ],
            desc="Train global + per-strategy models",
        )
        sync_models_to_user_data()

    if not args.skip_grid_train:
        run(
            [
                str(PY),
                str(ROOT / "simulation/scripts/export_grid_dataset.py"),
                "--pairs",
                "prod200",
                "--timerange",
                args.timerange,
                "--trade-source",
                "raw",
                "--scan-interval-min",
                "10",
                "--scan-workers",
                str(args.scan_workers),
                "--fresh",
            ],
            desc=f"Export grid trades · prod200 · raw · {args.timerange}",
        )
        run(
            [
                str(PY),
                str(ROOT / "simulation/scripts/compare_grid_models.py"),
                "--min-trades",
                "30",
            ],
            desc="Train + compare Grid models (all pairs export + trade_db)",
        )

    if not args.skip_compare:
        run(
            [str(PY), str(STUDY), "--phase", "compare"],
            desc="Compare no ML vs global vs per-strategy",
        )
        run(
            [str(PY), str(ROOT / "simulation/scripts/report_global_ml_by_month.py"), "--threshold", "0.6"],
            desc="Monthly global ML breakdown",
        )

    if not args.skip_report:
        run([str(PY), str(ROOT / "simulation/scripts/report_full_ml_study_txt.py")], desc="Write TXT report")

    print("\n=== PIPELINE DONE ===")
    print(f"  Report: {ROOT / 'simulation/results/full_ml_study/full_ml_study_report.txt'}")
    print(f"  Compare JSON: {ROOT / 'simulation/results/full_ml_study/comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
