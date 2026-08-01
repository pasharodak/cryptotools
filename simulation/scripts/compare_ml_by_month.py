#!/usr/bin/env python3
"""Compare portfolio PnL month-by-month: scanner-only vs ML entry gate."""
from __future__ import annotations

import calendar
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.exchange_sim.player_sync import summarize_trades  # noqa: E402
from simulation.scripts.select_player_pairs import pool_pairs  # noqa: E402

WORKERS = 3
MONTHS = [
    (2025, 11),
    (2025, 12),
    (2026, 1),
    (2026, 2),
    (2026, 3),
    (2026, 4),
    (2026, 5),
    (2026, 6),
]


def month_range(year: int, month: int) -> tuple[int, int, str]:
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=UTC)
    label = f"{year}-{month:02d}"
    timerange = f"{year}{month:02d}01-{year}{month:02d}{last:02d}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def aggregate_instances(instances: list[dict], *, use_ml: bool) -> dict:
    if use_ml:
        sum_key = "ml_summary"
    else:
        sum_key = "scanner_summary"
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
    skipped_wins = sum(1 for i in instances for t in i.get("ml_skipped_trades") or [] if float(t.get("profit_abs") or 0) >= 0)
    skipped_losses = sum(1 for i in instances for t in i.get("ml_skipped_trades") or [] if float(t.get("profit_abs") or 0) < 0)
    return {
        "trades": total_trades,
        "pnl_usdt": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0,
        "ml_skipped": skipped,
        "skipped_pnl_usdt": round(skipped_pnl, 4),
        "skipped_wins": skipped_wins,
        "skipped_losses": skipped_losses,
    }


def run_month(year: int, month: int, workers: int = WORKERS) -> dict:
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/ctengine"
    pairs = pool_pairs(ROOT)
    mgr = BotSessionManager(ROOT)
    gate = mgr._get_ml_gate()
    gate.set_enabled(False)

    mgr.init_live_session(pairs, start_ms, end_ms, datadir)
    pool = mgr.status.get("sim_pool") or pairs
    order = mgr.enabled_scenario_ids()

    def load_one(sid: str) -> str:
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(load_one, sid): sid for sid in order}
        for fut in as_completed(futs):
            fut.result()

    instances = list(mgr.instances)
    without_ml = aggregate_instances(instances, use_ml=False)
    with_ml = aggregate_instances(instances, use_ml=True)
    delta_pnl = round(with_ml["pnl_usdt"] - without_ml["pnl_usdt"], 4)
    delta_trades = with_ml["trades"] - without_ml["trades"]
    return {
        "month": label,
        "timerange": f"{start_ms}-{end_ms}",
        "bots": len(order),
        "pairs": len(pool),
        "without_ml": without_ml,
        "with_ml": with_ml,
        "delta_pnl_usdt": delta_pnl,
        "delta_trades": delta_trades,
        "better": "ml" if delta_pnl > 0 else ("tie" if delta_pnl == 0 else "scanner"),
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="ML gate vs scanner-only by month")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--month", help="Single month YYYY-MM")
    args = ap.parse_args()

    if args.month:
        y, m = map(int, args.month.split("-"))
        months = [(y, m)]
    else:
        months = MONTHS

    rows: list[dict] = []
    print("=== ML gate vs scanner (monthly) ===\n")
    print(f"{'Month':<8} {'Trades':>7} {'->ML':>6} {'PnL noML':>10} {'PnL ML':>10} {'Delta':>8} {'Skip':>5} {'SkipPnL':>8} Better")
    print("-" * 85)

    for y, m in months:
        print(f"  running {y}-{m:02d}...", flush=True)
        row = run_month(y, m, workers=args.workers)
        rows.append(row)
        w = row["without_ml"]
        ml = row["with_ml"]
        print(
            f"{row['month']:<8} "
            f"{w['trades']:>7} "
            f"{ml['trades']:>6} "
            f"{w['pnl_usdt']:>10.2f} "
            f"{ml['pnl_usdt']:>10.2f} "
            f"{row['delta_pnl_usdt']:>+8.2f} "
            f"{ml['ml_skipped']:>5} "
            f"{ml['skipped_pnl_usdt']:>8.2f} "
            f"{row['better']}"
        )

    total_wo = sum(r["without_ml"]["pnl_usdt"] for r in rows)
    total_ml = sum(r["with_ml"]["pnl_usdt"] for r in rows)
    total_skip = sum(r["with_ml"]["ml_skipped"] for r in rows)
    print("-" * 85)
    print(
        f"{'TOTAL':<8} {'':>7} {'':>6} {total_wo:>10.2f} {total_ml:>10.2f} {total_ml - total_wo:>+8.2f} {total_skip:>5}"
    )

    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "model": "xgboost entry gate (block predicted loss)",
        "months": rows,
        "total_without_ml_usdt": round(total_wo, 4),
        "total_with_ml_usdt": round(total_ml, 4),
        "total_delta_usdt": round(total_ml - total_wo, 4),
    }
    out_path = ROOT / "simulation/results/trade_db/models/ml_monthly_comparison.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
