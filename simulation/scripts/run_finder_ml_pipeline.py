#!/usr/bin/env python3
"""
Finder ML study pipeline (trend_12h feature):
  1. Train on prod200 pairs (Jan-Apr 2026)
  2. OOS backtest May-Jun 2026 (normal + inverted signals)
  3. Write JSON + TXT report
"""
from __future__ import annotations

import calendar
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.ml.market_features import manifest_datadir  # noqa: E402
from simulation.ml.trade_finder import (  # noqa: E402
    backtest_pairs,
    load_config,
    parse_timerange_ms,
    train_model,
    train_pairs,
)

FINDER_CONFIG = "simulation/config/finder_ml_study.json"
FINDER_MODEL_DIR = "simulation/results/finder_ml/models"
OUT_DIR = ROOT / "simulation/results/finder_ml"


def pairs_with_ohlcv(root: Path, pairs: list[str], timeframe: str = "5m") -> list[str]:
    datadir = manifest_datadir(root)
    ds = HistoricalDatastore(datadir)
    return [pair for pair in pairs if ds.has_pair(pair, timeframe)]


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    label = f"{year}-{month:02d}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def summarize_signals(signals: list[dict], start_ms: int, end_ms: int) -> dict:
    sigs = [s for s in signals if start_ms <= s["open_ms"] <= end_ms]
    pnl = sum(s["profit_abs"] for s in sigs)
    wins = sum(1 for s in sigs if s["profit_abs"] >= 0)
    n = len(sigs)
    return {
        "signals": n,
        "pnl_usdt": round(pnl, 4),
        "win_rate": round(wins / n, 4) if n else 0.0,
        "wins": wins,
        "losses": n - wins,
    }


def run_backtest_variant(
    root: Path,
    pairs: list[str],
    cfg: dict,
    *,
    label: str,
    invert: bool,
    workers: int,
) -> dict:
    test_tr = cfg.get("test_timerange", "20260501-20260630")
    test_start, test_end = parse_timerange_ms(test_tr)
    cfg_run = {**cfg, "invert_signal": invert}

    full = backtest_pairs(
        root,
        pairs,
        config_rel=FINDER_CONFIG,
        model_dir_rel=FINDER_MODEL_DIR,
        config_overrides={"invert_signal": invert},
        timerange=test_tr,
        use_classifier_gate=False,
        workers=workers,
    )

    months = []
    for y, m in ((2026, 5), (2026, 6)):
        s_ms, e_ms, mlabel = month_range(y, m)
        start_ms = max(s_ms, test_start)
        end_ms = min(e_ms, test_end)
        if start_ms >= end_ms:
            continue
        row = summarize_signals(full["signals"], start_ms, end_ms)
        row["month"] = mlabel
        months.append(row)

    full["variant"] = label
    full["invert_signal"] = invert
    full["months"] = months
    full["config"] = cfg_run
    return full


def write_report(
    train_meta: dict,
    normal: dict,
    inverted: dict,
    pairs: list[str],
    cfg: dict,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "study": "finder_ml_trend_12h",
        "trained_at": train_meta.get("trained_at"),
        "train_timerange": cfg.get("train_timerange"),
        "test_timerange": cfg.get("test_timerange"),
        "pairs_total": len(pairs),
        "train_metrics": {
            k: train_meta.get(k)
            for k in (
                "n_samples", "n_train", "n_test", "roc_auc", "accuracy",
                "profit_precision", "profit_recall", "positive_rate",
            )
        },
        "features": train_meta.get("features"),
        "backtest_normal": {
            "total_signals": normal["total_signals"],
            "total_pnl_usdt": normal["total_pnl_usdt"],
            "win_rate": normal["win_rate"],
            "months": normal["months"],
        },
        "backtest_inverted": {
            "total_signals": inverted["total_signals"],
            "total_pnl_usdt": inverted["total_pnl_usdt"],
            "win_rate": inverted["win_rate"],
            "months": inverted["months"],
        },
    }
    json_path = OUT_DIR / "finder_ml_study_report.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "=== Finder ML study (trend_12h) ===",
        f"Train: {cfg.get('train_timerange')}  |  Test OOS: {cfg.get('test_timerange')}",
        f"Pairs with OHLCV: {len(pairs)}",
        "",
        "Training (time-split holdout):",
        f"  samples: {train_meta.get('n_samples')}  ROC-AUC: {train_meta.get('roc_auc')}  "
        f"acc: {train_meta.get('accuracy')}",
        f"  profit prec/rec: {train_meta.get('profit_precision')}/{train_meta.get('profit_recall')}",
        "",
        f"OOS backtest May-Jun 2026 (invert=false):",
        f"  signals: {normal['total_signals']}  PnL: {normal['total_pnl_usdt']:+.2f} USDT  "
        f"WR: {normal['win_rate']*100:.1f}%",
    ]
    for row in normal["months"]:
        lines.append(
            f"    {row['month']}: {row['signals']} sig  {row['pnl_usdt']:+.2f} USDT  "
            f"WR {row['win_rate']*100:.1f}%"
        )
    lines.extend([
        "",
        f"OOS backtest May-Jun 2026 (invert=true):",
        f"  signals: {inverted['total_signals']}  PnL: {inverted['total_pnl_usdt']:+.2f} USDT  "
        f"WR: {inverted['win_rate']*100:.1f}%",
    ])
    for row in inverted["months"]:
        lines.append(
            f"    {row['month']}: {row['signals']} sig  {row['pnl_usdt']:+.2f} USDT  "
            f"WR {row['win_rate']*100:.1f}%"
        )
    txt_path = OUT_DIR / "finder_ml_study_report.txt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport: {txt_path}")
    print(f"JSON:   {json_path}")


def main() -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Finder ML study pipeline (trend_12h)")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, (os.cpu_count() or 4) - 1)),
        help="Parallel workers for dataset build and backtest (default: cpu_count-1, max 8)",
    )
    ap.add_argument(
        "--log-file",
        default="",
        help="Append stdout to this file (UTF-8, line-buffered)",
    )
    args = ap.parse_args()

    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

        class _Tee:
            def __init__(self, *streams):
                self.streams = streams

            def write(self, data: str) -> None:
                for s in self.streams:
                    s.write(data)

            def flush(self) -> None:
                for s in self.streams:
                    s.flush()

        sys.stdout = _Tee(sys.stdout, log_fh)  # type: ignore[assignment]

    cfg = load_config(ROOT, FINDER_CONFIG)
    all_pairs = train_pairs(ROOT, cfg)
    pairs = pairs_with_ohlcv(ROOT, all_pairs, cfg.get("timeframe", "5m"))
    print(f"=== Finder ML study ===", flush=True)
    print(f"Config: {FINDER_CONFIG}", flush=True)
    print(f"Pairs: {len(pairs)} / {len(all_pairs)} with OHLCV", flush=True)
    print(f"Train: {cfg['train_timerange']}  Test: {cfg.get('test_timerange')}", flush=True)
    print(f"Workers: {args.workers}", flush=True)
    print(f"Feature trend_12h: 12h return on 5m (144 bars)", flush=True)

    train_meta: dict = {}
    if not args.skip_train:
        print("\n--- Training ---", flush=True)
        train_meta = train_model(
            ROOT,
            config_rel=FINDER_CONFIG,
            model_dir_rel=FINDER_MODEL_DIR,
            workers=args.workers,
        )
        print(f"Model -> {ROOT / FINDER_MODEL_DIR / 'trade_finder.joblib'}", flush=True)
    else:
        meta_path = ROOT / FINDER_MODEL_DIR / "trade_finder_meta.json"
        if meta_path.is_file():
            train_meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if args.skip_backtest:
        return 0

    print("\n--- OOS backtest (normal) ---", flush=True)
    normal = run_backtest_variant(ROOT, pairs, cfg, label="normal", invert=False, workers=args.workers)

    print("\n--- OOS backtest (inverted) ---", flush=True)
    inverted = run_backtest_variant(ROOT, pairs, cfg, label="inverted", invert=True, workers=args.workers)

    write_report(train_meta, normal, inverted, pairs, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
