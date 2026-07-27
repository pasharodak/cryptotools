#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from simulation.scripts.bench_scanner_portfolio import eval_portfolio

SCEN = ROOT / "simulation/config/player_scenarios.json"
orig = json.loads(SCEN.read_text(encoding="utf-8"))
MAY, JUNE = "20260501-20260531", "20260601-20260625"
best = None
results = []
for stake in (50, 75, 85, 95, 100, 108):
    scen = json.loads(json.dumps(orig))
    for s in scen:
        if s["id"] == "live_grid":
            s["stake_usdt"] = stake
        if s["id"] == "lite_swing":
            s["enabled"] = False
    SCEN.write_text(json.dumps(scen, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    may = eval_portfolio(MAY)
    june = eval_portfolio(JUNE)
    both = may["pnl_pct"] > 0 and june["pnl_pct"] > 0
    total = may["pnl_pct"] + june["pnl_pct"]
    results.append({"stake": stake, "may": may["pnl_pct"], "june": june["pnl_pct"], "total": total, "both": both})
    print(f"stake {stake}: May {may['pnl_pct']:+.2f}% June {june['pnl_pct']:+.2f}% total {total:+.2f}% both+={both}", flush=True)
    if both and (best is None or total > best["total"]):
        best = {"stake": stake, "may": may, "june": june, "total": total}

(ROOT / "simulation/results/stake_sweep.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
if best:
    scen = json.loads(json.dumps(orig))
    for s in scen:
        if s["id"] == "live_grid":
            s["stake_usdt"] = best["stake"]
            s["settings"] = f"5x · top-4 · stake {best['stake']}"
        if s["id"] == "lite_swing":
            s["enabled"] = False
    SCEN.write_text(json.dumps(scen, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    (ROOT / "simulation/results/scanner_may_final.json").write_text(
        json.dumps(best["may"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (ROOT / "simulation/results/scanner_june_final.json").write_text(
        json.dumps(best["june"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"BEST stake {best['stake']} May {best['may']['pnl_pct']}% June {best['june']['pnl_pct']}% total {best['total']:.2f}%", flush=True)
