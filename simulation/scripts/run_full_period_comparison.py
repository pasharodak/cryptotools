#!/usr/bin/env python3
"""Full-period comparison: bots (no ML / with ML) + trade finder only."""
from __future__ import annotations

import calendar
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.exchange_sim.player_sync import summarize_trades  # noqa: E402
from simulation.ml.trade_finder import backtest_pairs, load_config  # noqa: E402
from simulation.scripts.comparison_common import (  # noqa: E402
    DEFAULT_TIMERANGE,
    pairs_from_source,
    period_months,
)

WORKERS = 3
OUT_DIR = ROOT / "simulation/results/trade_db/models"


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), f"{year}-{month:02d}"


def aggregate_instances(instances: list[dict], *, use_ml: bool) -> dict:
    sum_key = "ml_summary" if use_ml else "scanner_summary"
    total_trades = 0
    total_pnl = 0.0
    wins = 0
    losses = 0
    for inst in instances:
        s = inst.get(sum_key) or summarize_trades(
            inst.get("scanner_trades" if not use_ml else inst.get("trades") or [])
        )
        total_trades += int(s.get("total_trades") or 0)
        total_pnl += float(s.get("profit_abs") or 0)
        wins += int(s.get("wins") or 0)
        losses += int(s.get("losses") or 0)
    skipped = sum(int((i.get("ml_gate") or {}).get("skipped") or 0) for i in instances)
    skipped_pnl = sum(float(t.get("profit_abs") or 0) for i in instances for t in i.get("ml_skipped_trades") or [])
    return {
        "trades": total_trades,
        "pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0,
        "ml_skipped": skipped,
        "skipped_pnl_usdt": round(skipped_pnl, 4),
    }


def run_bots_month(pairs: list[str], year: int, month: int, workers: int) -> dict:
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/freqtrade"
    mgr = BotSessionManager(ROOT)
    mgr._get_ml_gate().set_enabled(False)
    mgr.init_live_session(pairs, start_ms, end_ms, datadir)
    pool = mgr.status.get("sim_pool") or pairs
    order = mgr.enabled_scenario_ids()

    def load_one(sid: str) -> str:
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed({ex.submit(load_one, sid): sid for sid in order}):
            fut.result()

    instances = list(mgr.instances)
    without_ml = aggregate_instances(instances, use_ml=False)
    with_ml = aggregate_instances(instances, use_ml=True)
    return {
        "month": label,
        "pairs": len(pool),
        "bots": len(order),
        "without_ml": without_ml,
        "with_ml": with_ml,
        "delta_pnl_usdt": round(with_ml["pnl_usdt"] - without_ml["pnl_usdt"], 4),
        "better": "ml" if with_ml["pnl_usdt"] > without_ml["pnl_usdt"] else "scanner",
    }


def run_bots_comparison(pairs: list[str], months: list[tuple[int, int]], workers: int, out_path: Path) -> dict:
    rows: list[dict] = []
    partial = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}
    done = {r["month"] for r in partial.get("months") or []}
    rows = list(partial.get("months") or [])

    print("\n=== BOTS: scanner vs ML gate ===")
    print(f"{'Month':<8} {'Pairs':>5} {'Trades':>7} {'->ML':>6} {'PnL no':>9} {'PnL ML':>9} {'Delta':>8}")
    print("-" * 62)
    for y, m in months:
        label = f"{y}-{m:02d}"
        if label in done:
            r = next(x for x in rows if x["month"] == label)
            w, ml = r["without_ml"], r["with_ml"]
            print(f"{label:<8} {r['pairs']:>5} {w['trades']:>7} {ml['trades']:>6} {w['pnl_usdt']:>9.2f} {ml['pnl_usdt']:>9.2f} {r['delta_pnl_usdt']:>+8.2f} (cached)")
            continue
        print(f"  {label}...", flush=True)
        row = run_bots_month(pairs, y, m, workers)
        rows.append(row)
        w, ml = row["without_ml"], row["with_ml"]
        print(
            f"{label:<8} {row['pairs']:>5} {w['trades']:>7} {ml['trades']:>6} "
            f"{w['pnl_usdt']:>9.2f} {ml['pnl_usdt']:>9.2f} {row['delta_pnl_usdt']:>+8.2f}"
        )
        partial_body = {
            "compared_at": datetime.now(tz=UTC).isoformat(),
            "timerange": DEFAULT_TIMERANGE,
            "pairs": pairs,
            "pair_count": len(pairs),
            "months": rows,
            "partial": True,
        }
        out_path.write_text(json.dumps(partial_body, indent=2, ensure_ascii=False), encoding="utf-8")

    total_wo = sum(r["without_ml"]["pnl_usdt"] for r in rows)
    total_ml = sum(r["with_ml"]["pnl_usdt"] for r in rows)
    print("-" * 62)
    print(f"{'TOTAL':<8} {'':>5} {'':>7} {'':>6} {total_wo:>9.2f} {total_ml:>9.2f} {total_ml - total_wo:>+8.2f}")
    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "model": "xgboost entry gate (block predicted loss)",
        "timerange": DEFAULT_TIMERANGE,
        "pairs": pairs,
        "pair_count": len(pairs),
        "months": rows,
        "total_without_ml_usdt": round(total_wo, 4),
        "total_with_ml_usdt": round(total_ml, 4),
        "total_delta_usdt": round(total_ml - total_wo, 4),
        "partial": False,
    }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def run_finder_comparison(pairs: list[str], months: list[tuple[int, int]], out_path: Path) -> dict:
    rows: list[dict] = []
    partial = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}
    done = {r["month"] for r in partial.get("months") or []}
    rows = list(partial.get("months") or [])

    cfg = load_config(ROOT)
    scan_min = int(cfg.get("scan_stride") or 4) * 5
    print(f"\n=== FINDER ONLY (scan every {scan_min} min) ===")
    print(f"{'Month':<8} {'Signals':>8} {'PnL':>10} {'Win%':>6}")
    print("-" * 36)
    for y, m in months:
        label = f"{y}-{m:02d}"
        if label in done:
            r = next(x for x in rows if x["month"] == label)
            print(f"{label:<8} {r['signals']:>8} {r['pnl_usdt']:>+10.2f} {r['win_rate']*100:>5.1f}% (cached)")
            continue
        print(f"  {label}...", flush=True)
        s_ms, e_ms, _ = month_range(y, m)
        rep = backtest_pairs(ROOT, pairs, start_ms=s_ms, end_ms=e_ms, use_classifier_gate=False)
        row = {
            "month": label,
            "signals": rep["total_signals"],
            "pnl_usdt": rep["total_pnl_usdt"],
            "win_rate": rep["win_rate"],
            "wins": rep["wins"],
            "losses": rep["losses"],
        }
        rows.append(row)
        print(f"{label:<8} {row['signals']:>8} {row['pnl_usdt']:>+10.2f} {row['win_rate']*100:>5.1f}%")
        out_path.write_text(
            json.dumps({"months": rows, "partial": True, "pairs": pairs}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    total_pnl = sum(r["pnl_usdt"] for r in rows)
    total_sig = sum(r["signals"] for r in rows)
    print("-" * 36)
    print(f"{'TOTAL':<8} {total_sig:>8} {total_pnl:>+10.2f}")
    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "timerange": DEFAULT_TIMERANGE,
        "pairs": pairs,
        "pair_count": len(pairs),
        "scan_minutes": scan_min,
        "months": rows,
        "total_pnl_usdt": round(total_pnl, 4),
        "total_signals": total_sig,
        "partial": False,
    }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Full period: bots ML + finder")
    ap.add_argument("--pairs", choices=["all", "pool"], default="all")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-bots", action="store_true")
    ap.add_argument("--skip-finder", action="store_true")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--from-month", default="2025-01")
    ap.add_argument("--to-month", default="2026-07")
    args = ap.parse_args()

    fy, fm = map(int, args.from_month.split("-"))
    ty, tm = map(int, args.to_month.split("-"))
    months = period_months(fy, fm, ty, tm)

    if not args.skip_download:
        print("=== DOWNLOAD all pairs from Jan 2025 ===")
        r = subprocess.run(
            [sys.executable, str(ROOT / "simulation/scripts/download_all_pairs_history.py")],
            cwd=str(ROOT),
        )
        if r.returncode != 0:
            print("WARN: download had errors")

    pairs = pairs_from_source(ROOT, args.pairs, min_start="20250101")
    print(f"\nPairs with 5m data from Jan 2025: {len(pairs)}")
    if not pairs:
        print("No pairs — run download first")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.pairs
    summary: dict = {"pairs": pairs, "pair_count": len(pairs), "months": f"{args.from_month}..{args.to_month}"}

    if not args.skip_bots:
        bots_path = OUT_DIR / f"full_period_bots_{tag}.json"
        summary["bots"] = run_bots_comparison(pairs, months, args.workers, bots_path)
        print(f"Report: {bots_path}")

    if not args.skip_finder:
        finder_path = OUT_DIR / f"full_period_finder_{tag}.json"
        summary["finder"] = run_finder_comparison(pairs, months, finder_path)
        print(f"Report: {finder_path}")

    summary_path = OUT_DIR / f"full_period_summary_{tag}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSummary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
