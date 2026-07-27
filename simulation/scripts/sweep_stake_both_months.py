#!/usr/bin/env python3
"""Sweep grid stake — maximize May+June while both positive."""
from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.scripts.bench_scanner_portfolio import eval_portfolio  # noqa: E402

SCEN_PATH = ROOT / "simulation/config/player_scenarios.json"
CSV_PATH = ROOT / "simulation/results/bot_iteration_stats.csv"
MAY, JUNE = "20260501-20260531", "20260601-20260625"


def load_scen():
    return json.loads(SCEN_PATH.read_text(encoding="utf-8"))


def save_scen(data):
    SCEN_PATH.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def log_row(row: dict) -> None:
    fields = list(row.keys())
    new = not CSV_PATH.is_file()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows([row])


def main() -> int:
    orig = load_scen()
    results = []
    for stake in (50, 60, 70, 75, 80, 85, 90, 95, 100, 108):
        scen = json.loads(json.dumps(orig))
        for s in scen:
            if s["id"] == "live_grid":
                s["stake_usdt"] = stake
                s["enabled"] = True
            if s["id"] == "lite_swing":
                s["enabled"] = False
        save_scen(scen)
        may = eval_portfolio(MAY)
        june = eval_portfolio(JUNE)
        both = may["pnl_pct"] > 0 and june["pnl_pct"] > 0
        total = may["pnl_pct"] + june["pnl_pct"]
        r = {
            "stake": stake,
            "may_pct": may["pnl_pct"],
            "june_pct": june["pnl_pct"],
            "total_pct": round(total, 2),
            "both_positive": both,
        }
        results.append(r)
        print(f"stake {stake}: May {may['pnl_pct']:+.2f}% June {june['pnl_pct']:+.2f}% total {total:+.2f}% both+={both}")
        if both:
            ts = datetime.now(UTC).isoformat()
            for phase, rep in (("may_tune", may), ("june_validate", june)):
                for s in rep["strategies"]:
                    log_row(
                        {
                            "iteration": f"stake_sweep_{stake}",
                            "timestamp_utc": ts,
                            "phase": phase,
                            "change_summary": f"grid stake {stake}, swing off",
                            "sources": "stake sweep for higher PnL",
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
                                f"{p.get('pair')}:{p.get('profit_abs'):+.2f}"
                                for p in (s.get("top_pairs") or [])[:4]
                            ),
                            "enabled": True,
                        }
                    )

    save_scen(orig)
    good = sorted([r for r in results if r["both_positive"]], key=lambda x: -x["total_pct"])
    out = ROOT / "simulation/results/stake_sweep.json"
    out.write_text(json.dumps({"all": results, "best": good[:5]}, indent=2), encoding="utf-8")
    if good:
        best = good[0]
        print(f"\nBEST: stake {best['stake']} — May {best['may_pct']}% June {best['june_pct']}% total {best['total_pct']}%")
        scen = json.loads(json.dumps(orig))
        for s in scen:
            if s["id"] == "live_grid":
                s["stake_usdt"] = best["stake"]
                s["settings"] = f"5x · top-4 · streak≥2 · stake {best['stake']}"
            if s["id"] == "lite_swing":
                s["enabled"] = False
        save_scen(scen)
        may = eval_portfolio(MAY)
        june = eval_portfolio(JUNE)
        (ROOT / "simulation/results/scanner_may_final.json").write_text(
            json.dumps(may, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (ROOT / "simulation/results/scanner_june_final.json").write_text(
            json.dumps(june, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
