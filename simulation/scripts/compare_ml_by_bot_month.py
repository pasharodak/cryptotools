#!/usr/bin/env python3
"""Per-bot monthly stats: scanner-only vs ML entry gate."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager  # noqa: E402
from simulation.scripts.compare_ml_by_month import (  # noqa: E402
    MONTHS,
    aggregate_instances,
    month_range,
    pool_pairs,
)

OUT_PATH = ROOT / "simulation/results/trade_db/models/ml_bot_monthly_comparison.json"


def bots_from_month(instances: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    labels: dict[str, str] = {}
    for inst in instances:
        sid = inst["scenario_id"]
        grouped[sid].append(inst)
        labels[sid] = inst.get("label") or sid

    bots: list[dict] = []
    for sid in sorted(grouped):
        insts = grouped[sid]
        wo = aggregate_instances(insts, use_ml=False)
        ml = aggregate_instances(insts, use_ml=True)
        delta = round(ml["pnl_usdt"] - wo["pnl_usdt"], 4)
        bots.append(
            {
                "scenario_id": sid,
                "label": labels[sid],
                "without_ml": wo,
                "with_ml": ml,
                "delta_pnl_usdt": delta,
                "delta_trades": ml["trades"] - wo["trades"],
                "better": "ml" if delta > 0 else ("tie" if delta == 0 else "scanner"),
            }
        )
    return bots


def run_month_with_bots(year: int, month: int, workers: int) -> dict:
    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/freqtrade"
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
    row = {
        "month": label,
        "without_ml": aggregate_instances(instances, use_ml=False),
        "with_ml": aggregate_instances(instances, use_ml=True),
        "bots": bots_from_month(instances),
    }
    row["delta_pnl_usdt"] = round(row["with_ml"]["pnl_usdt"] - row["without_ml"]["pnl_usdt"], 4)
    return row


def print_bot_table(month: str, bots: list[dict]) -> None:
    print(f"\n--- {month} ---")
    print(f"{'Bot':<28} {'Tr':>5} {'->ML':>5} {'PnL no':>9} {'PnL ML':>9} {'Delta':>8} {'Skip':>5} Better")
    print("-" * 82)
    for b in sorted(bots, key=lambda x: x["delta_pnl_usdt"], reverse=True):
        w, ml = b["without_ml"], b["with_ml"]
        name = (b["label"] or b["scenario_id"])[:28]
        print(
            f"{name:<28} "
            f"{w['trades']:>5} "
            f"{ml['trades']:>5} "
            f"{w['pnl_usdt']:>9.2f} "
            f"{ml['pnl_usdt']:>9.2f} "
            f"{b['delta_pnl_usdt']:>+8.2f} "
            f"{ml['ml_skipped']:>5} "
            f"{b['better']}"
        )


def build_bot_totals(month_rows: list[dict]) -> list[dict]:
    acc: dict[str, dict] = {}
    for row in month_rows:
        for b in row["bots"]:
            sid = b["scenario_id"]
            if sid not in acc:
                acc[sid] = {
                    "scenario_id": sid,
                    "label": b["label"],
                    "months": [],
                    "total_without_ml_usdt": 0.0,
                    "total_with_ml_usdt": 0.0,
                    "total_trades_no_ml": 0,
                    "total_trades_ml": 0,
                    "total_skipped": 0,
                }
            a = acc[sid]
            a["months"].append(
                {
                    "month": row["month"],
                    "without_ml": b["without_ml"],
                    "with_ml": b["with_ml"],
                    "delta_pnl_usdt": b["delta_pnl_usdt"],
                    "better": b["better"],
                }
            )
            a["total_without_ml_usdt"] += b["without_ml"]["pnl_usdt"]
            a["total_with_ml_usdt"] += b["with_ml"]["pnl_usdt"]
            a["total_trades_no_ml"] += b["without_ml"]["trades"]
            a["total_trades_ml"] += b["with_ml"]["trades"]
            a["total_skipped"] += b["with_ml"]["ml_skipped"]

    totals = []
    for sid in sorted(acc):
        a = acc[sid]
        delta = round(a["total_with_ml_usdt"] - a["total_without_ml_usdt"], 4)
        totals.append(
            {
                **a,
                "total_without_ml_usdt": round(a["total_without_ml_usdt"], 4),
                "total_with_ml_usdt": round(a["total_with_ml_usdt"], 4),
                "total_delta_usdt": delta,
                "better": "ml" if delta > 0 else ("tie" if delta == 0 else "scanner"),
            }
        )
    return sorted(totals, key=lambda x: x["total_delta_usdt"], reverse=True)


def save_partial(month_rows: list[dict]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "model": "xgboost entry gate (block predicted loss)",
        "months": month_rows,
        "bot_totals": build_bot_totals(month_rows),
        "partial": len(month_rows) < len(MONTHS),
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="ML gate vs scanner by bot and month")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--month", help="Single month YYYY-MM")
    ap.add_argument("--from-month", help="Start from YYYY-MM (inclusive)")
    args = ap.parse_args()

    months = list(MONTHS)
    if args.month:
        y, m = map(int, args.month.split("-"))
        months = [(y, m)]
    elif args.from_month:
        y0, m0 = map(int, args.from_month.split("-"))
        months = [(y, m) for y, m in MONTHS if (y, m) >= (y0, m0)]

    month_rows: list[dict] = []
    if OUT_PATH.is_file() and args.from_month:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        month_rows = [r for r in prev.get("months", []) if r["month"] < args.from_month]

    print("=== ML gate vs scanner — by bot, monthly ===")

    for y, m in months:
        label = f"{y}-{m:02d}"
        if any(r["month"] == label for r in month_rows):
            print(f"\n  skip {label} (already done)", flush=True)
            continue
        print(f"\n  running {label}...", flush=True)
        row = run_month_with_bots(y, m, workers=args.workers)
        month_rows.append(row)
        month_rows.sort(key=lambda r: r["month"])
        save_partial(month_rows)
        print_bot_table(row["month"], row["bots"])

    month_rows.sort(key=lambda r: r["month"])
    bot_totals = build_bot_totals(month_rows)

    print("\n=== TOTAL by bot (all months) ===")
    print(f"{'Bot':<28} {'Tr no':>6} {'Tr ML':>6} {'PnL no':>10} {'PnL ML':>10} {'Delta':>9} {'Skip':>6}")
    print("-" * 88)
    for b in bot_totals:
        name = (b["label"] or b["scenario_id"])[:28]
        print(
            f"{name:<28} "
            f"{b['total_trades_no_ml']:>6} "
            f"{b['total_trades_ml']:>6} "
            f"{b['total_without_ml_usdt']:>10.2f} "
            f"{b['total_with_ml_usdt']:>10.2f} "
            f"{b['total_delta_usdt']:>+9.2f} "
            f"{b['total_skipped']:>6}"
        )

    out = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "model": "xgboost entry gate (block predicted loss)",
        "months": month_rows,
        "bot_totals": bot_totals,
        "partial": False,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
