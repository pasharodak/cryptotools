#!/usr/bin/env python3
"""Benchmark all bots including new trend strategies; log CSV."""
from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.scripts.bench_scanner_portfolio import eval_portfolio  # noqa: E402

CSV = ROOT / "simulation/results/bot_iteration_stats.csv"
MAY, JUNE = "20260501-20260531", "20260601-20260625"
FIELDS = [
    "iteration", "timestamp_utc", "phase", "change_summary", "sources",
    "portfolio_pnl_usdt", "portfolio_pnl_pct", "bot_id", "bot_label",
    "stake_usdt", "trades", "wins", "losses", "profit_usdt",
    "armed_pairs", "traded_pairs", "top_pairs", "enabled",
]


def log(phase: str, rep: dict) -> None:
    ts = datetime.now(UTC).isoformat()
    new = not CSV.is_file()
    with CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for s in rep["strategies"]:
            w.writerow({
                "iteration": "trend_bots_v1",
                "timestamp_utc": ts,
                "phase": phase,
                "change_summary": "5 trend strategies + grid108 + arb",
                "sources": "user trend/breakout/fib/liquidity/channel specs",
                "portfolio_pnl_usdt": rep["pnl_usdt"],
                "portfolio_pnl_pct": rep["pnl_pct"],
                "bot_id": s["id"],
                "bot_label": s["label"],
                "stake_usdt": s.get("stake_usdt"),
                "trades": s.get("total_trades"),
                "wins": s.get("wins"),
                "losses": s.get("losses"),
                "profit_usdt": s.get("profit_abs"),
                "armed_pairs": s.get("armed_pairs"),
                "traded_pairs": s.get("traded_pairs"),
                "top_pairs": "; ".join(
                    f"{p.get('pair')}:{p.get('profit_abs'):+.2f}" for p in (s.get("top_pairs") or [])[:4]
                ),
                "enabled": True,
            })


def main() -> int:
    may = eval_portfolio(MAY)
    june = eval_portfolio(JUNE)
    log("may_tune", may)
    log("june_validate", june)
    out = {
        "may": may,
        "june": june,
        "both_positive": may["pnl_pct"] > 0 and june["pnl_pct"] > 0,
    }
    (ROOT / "simulation/results/trend_bots_bench.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({
        "may_pct": may["pnl_pct"],
        "june_pct": june["pnl_pct"],
        "bots": [{ "id": s["id"], "may_june_note": s["label"], "trades_may": s["total_trades"]} for s in may["strategies"]],
    }, indent=2))
    print(f"May {may['pnl_pct']:+.2f}% June {june['pnl_pct']:+.2f}%")
    for s in june["strategies"]:
        print(f"  {s['id']}: {s['profit_abs']:+.2f} USDT ({s['total_trades']} trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
