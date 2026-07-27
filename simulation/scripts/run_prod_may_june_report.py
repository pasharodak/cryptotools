#!/usr/bin/env python3
"""Prod bots (Grid + 5 strategies) — May/June sim report, no ML Finder."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.scripts.apply_prod_ml_config import main as apply_prod  # noqa: E402
from simulation.scripts.compare_ml_by_bot_month import (  # noqa: E402
    aggregate_instances,
    build_bot_totals,
    print_bot_table,
    run_month_with_bots,
)

OUT_PATH = ROOT / "simulation/results/prod_may_june_2026.json"
PROD_CFG = ROOT / "simulation/config/prod_ml_bots.json"
MONTHS = [(2026, 5), (2026, 6)]


def _sum_bots(bots: list[dict], *, use_ml: bool) -> dict:
    key = "with_ml" if use_ml else "without_ml"
    trades = sum(int(b[key]["trades"]) for b in bots)
    pnl = round(sum(float(b[key]["pnl_usdt"]) for b in bots), 4)
    wins = sum(int(b[key]["wins"]) for b in bots)
    losses = sum(int(b[key]["losses"]) for b in bots)
    skipped = sum(int(b["with_ml"]["ml_skipped"]) for b in bots)
    return {
        "trades": trades,
        "pnl_usdt": pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / trades, 4) if trades else 0,
        "ml_skipped": skipped,
    }


def filter_row(row: dict, enabled: set[str]) -> dict:
    bots = [b for b in row["bots"] if b["scenario_id"] in enabled]
    out = dict(row)
    out["bots"] = bots
    out["without_ml"] = _sum_bots(bots, use_ml=False)
    out["with_ml"] = _sum_bots(bots, use_ml=True)
    out["delta_pnl_usdt"] = round(out["with_ml"]["pnl_usdt"] - out["without_ml"]["pnl_usdt"], 4)
    return out


def main() -> int:
    apply_prod()
    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled = set(prod["enabled_scenarios"])
    ml_gate = prod.get("ml_gate") or {}

    print("=== Prod sim May–June 2026 (no ML Finder) ===")
    print(f"Bots: {', '.join(sorted(enabled))}")
    print(
        f"ML gate: Grid={ml_gate.get('gate_mode')} · "
        f"Strategies={ml_gate.get('strategy_bots', {})}"
    )

    month_rows: list[dict] = []
    for y, m in MONTHS:
        label = f"{y}-{m:02d}"
        print(f"\n  running {label}...", flush=True)
        row = filter_row(run_month_with_bots(y, m, workers=3), enabled)
        month_rows.append(row)
        print_bot_table(row["month"], row["bots"])

    bot_totals = build_bot_totals(month_rows)
    bot_totals = [b for b in bot_totals if b["scenario_id"] in enabled]

    total_no_ml = round(sum(r["without_ml"]["pnl_usdt"] for r in month_rows), 4)
    total_ml = round(sum(r["with_ml"]["pnl_usdt"] for r in month_rows), 4)
    total_trades_no = sum(r["without_ml"]["trades"] for r in month_rows)
    total_trades_ml = sum(r["with_ml"]["trades"] for r in month_rows)
    total_skipped = sum(r["with_ml"]["ml_skipped"] for r in month_rows)

    print("\n=== TOTAL portfolio (May + June) ===")
    print(f"  Scanner only: {total_trades_no} trades · {total_no_ml:+.2f} USDT")
    print(f"  With ML gate: {total_trades_ml} trades · {total_ml:+.2f} USDT")
    print(f"  ML delta: {total_ml - total_no_ml:+.2f} USDT · skipped {total_skipped} trades")

    print("\n=== TOTAL by bot ===")
    print(f"{'Bot':<28} {'Tr no':>6} {'Tr ML':>6} {'PnL no':>10} {'PnL ML':>10} {'Delta':>9}")
    print("-" * 78)
    for b in bot_totals:
        name = (b["label"] or b["scenario_id"])[:28]
        print(
            f"{name:<28} "
            f"{b['total_trades_no_ml']:>6} "
            f"{b['total_trades_ml']:>6} "
            f"{b['total_without_ml_usdt']:>10.2f} "
            f"{b['total_with_ml_usdt']:>10.2f} "
            f"{b['total_delta_usdt']:>+9.2f}"
        )

    out = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "period": "2026-05 — 2026-06",
        "bots": sorted(enabled),
        "ml_gate": ml_gate,
        "excluded": ["live_freqai", "TradeFinderStrategy"],
        "months": month_rows,
        "bot_totals": bot_totals,
        "portfolio": {
            "without_ml": {
                "trades": total_trades_no,
                "pnl_usdt": total_no_ml,
            },
            "with_ml": {
                "trades": total_trades_ml,
                "pnl_usdt": total_ml,
                "skipped": total_skipped,
            },
            "delta_pnl_usdt": round(total_ml - total_no_ml, 4),
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
