#!/usr/bin/env python3
"""Sweep scanner params — both May and June must be positive."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.scripts.bench_scanner_portfolio import eval_portfolio  # noqa: E402

SCAN_PATH = ROOT / "simulation/config/sim_player_scan.json"
SCEN_PATH = ROOT / "simulation/config/player_scenarios.json"


def load_json(p: Path) -> dict | list:
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def run_variant(name: str, scan_patch: dict, stake: int, streak: int, min_loss: float) -> dict:
    scan = load_json(SCAN_PATH)
    scen = load_json(SCEN_PATH)
    r = scan.setdefault("ranging", {})
    r.update(scan_patch)
    r["trade_skip_loss_streak"] = streak
    r["pnl_circuit_min_loss_usdt"] = min_loss
    for s in scen:
        if s.get("id") == "live_grid":
            s["stake_usdt"] = stake
    save_json(SCAN_PATH, scan)
    save_json(SCEN_PATH, scen)
    may = eval_portfolio("20260501-20260531")
    june = eval_portfolio("20260601-20260625")
    return {
        "name": name,
        "stake": stake,
        "streak": streak,
        "min_loss": min_loss,
        "may_pct": may["pnl_pct"],
        "june_pct": june["pnl_pct"],
        "may_usdt": may["pnl_usdt"],
        "june_usdt": june["pnl_usdt"],
        "both_pos": may["pnl_pct"] > 0 and june["pnl_pct"] > 0,
    }


def main() -> int:
    orig_scan = load_json(SCAN_PATH)
    orig_scen = load_json(SCEN_PATH)
    base_patch = {
        "exclude_bases": ["BTC", "ETH", "BNB", "INJ", "TIA", "SEI", "OP", "APT", "SUI"],
        "max_pairs": 4,
        "whitelist_grace_scans": 2,
    }
    variants = []
    for stake in (50, 55, 60, 65, 70, 75):
        for streak in (0, 2, 3):
            for min_loss in (0.0, 0.5, 1.0):
                if streak == 0 and min_loss > 0:
                    continue
                name = f"s{stake}_st{streak}_ml{min_loss}"
                print(f"Testing {name}...", flush=True)
                try:
                    variants.append(run_variant(name, base_patch, stake, streak, min_loss))
                except Exception as exc:
                    variants.append({"name": name, "error": str(exc)})
    save_json(SCAN_PATH, orig_scan)
    save_json(SCEN_PATH, orig_scen)
    good = [v for v in variants if v.get("both_pos")]
    good.sort(key=lambda x: -(x["may_pct"] + x["june_pct"]))
    out = ROOT / "simulation/results/sweep_both_months.json"
    out.write_text(json.dumps({"all": variants, "best": good[:10]}, indent=2), encoding="utf-8")
    print("\n=== BOTH POSITIVE ===")
    for v in good[:8]:
        print(f"{v['name']}: May {v['may_pct']:+.2f}%  June {v['june_pct']:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
