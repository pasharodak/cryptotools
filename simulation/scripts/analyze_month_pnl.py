#!/usr/bin/env python3
from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
pbt = ROOT / "simulation/results/player_backtests"
scenarios = json.loads((ROOT / "simulation/config/player_scenarios.json").read_text(encoding="utf-8"))


def pnl_by_pair(zip_path: Path, strat: str) -> dict[str, float]:
    if not zip_path.is_file():
        return {}
    with zipfile.ZipFile(zip_path) as zf:
        js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(js))
    trades = data.get("strategy", {}).get(strat, {}).get("trades") or []
    by: dict[str, float] = defaultdict(float)
    for t in trades:
        by[t["pair"]] += float(t["profit_abs"])
    return dict(by)


print("=== Month breakdown (player backtests if available) ===\n")
for sc in scenarios:
    sid = sc["id"]
    zips = sorted(pbt.glob(f"player_{sid}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        print(f"{sc['label']}: no backtest zip yet")
        continue
    by = pnl_by_pair(zips[0], sc["strategy"])
    if not by:
        print(f"{sc['label']}: 0 trades")
        continue
    total = sum(by.values())
    worst = sorted(by.items(), key=lambda x: x[1])[:3]
    best = sorted(by.items(), key=lambda x: x[1], reverse=True)[:2]
    def sym(p: str) -> str:
        return p.split("/")[0]

    print(f"{sc['label']} ({sc.get('stake_usdt', '?')} USDT stake): {total:+.4f} USDT total")
    print(f"  worst: {', '.join(f'{sym(p)} {v:+.4f}' for p, v in worst)}")
    print(f"  best:  {', '.join(f'{sym(p)} {v:+.4f}' for p, v in best)}")
    print()
