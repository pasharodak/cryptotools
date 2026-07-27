#!/usr/bin/env python3
"""Rank player pairs by combined backtest PnL across Lite strategies."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402

TIMERANGE = "20260601-20260625"


def run_backtest(sc: dict, pairs: list[str]) -> dict[str, float]:
    sid = sc["id"]
    runtime = ROOT / "simulation/data/runtime" / f"rank_{sid}.json"
    patch_whitelist(ROOT, sc["config"], pairs, runtime)
    ft = ROOT / ".venv/Scripts/freqtrade.exe"
    out_dir = ROOT / "simulation/results/pair_ranking"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(runtime),
        "--strategy",
        sc["strategy"],
        "--strategy-path",
        str(ROOT / sc["strategy_path"]),
        "--datadir",
        str(ROOT / "simulation/data/freqtrade"),
        "--timerange",
        TIMERANGE,
        "--export",
        "trades",
        "--export-filename",
        f"rank_{sid}",
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    last = out_dir / ".last_result.json"
    zip_path = None
    if last.is_file():
        latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest")
        if latest:
            zip_path = out_dir / latest
    by_pair: dict[str, float] = defaultdict(float)
    if zip_path and zip_path.is_file():
        with zipfile.ZipFile(zip_path) as zf:
            js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
            data = json.loads(zf.read(js))
        for t in data.get("strategy", {}).get(sc["strategy"], {}).get("trades") or []:
            by_pair[t["pair"]] += float(t["profit_abs"])
    return dict(by_pair)


def main() -> int:
    cfg = json.loads((ROOT / "simulation/config/manifest.json").read_text(encoding="utf-8"))
    pool = json.loads((ROOT / "simulation/config/player_pair_pool.json").read_text(encoding="utf-8")).get("pairs") or []
    if not pool:
        pool = cfg.get("player_pairs") or []
    scenarios = json.loads((ROOT / "simulation/config/player_scenarios.json").read_text(encoding="utf-8"))
    lite = [s for s in scenarios if s.get("id") == "live_grid" or (s.get("group") == "lite" and s.get("enabled"))]
    combined: dict[str, float] = defaultdict(float)
    for sc in lite:
        print(f"  backtest {sc['id']}…")
        for pair, pnl in run_backtest(sc, pool).items():
            combined[pair] += pnl
    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    print("\nPair ranking (combined Lite PnL, USDT):")
    for pair, pnl in ranked:
        print(f"  {pair.split('/')[0]:6} {pnl:+.4f}")
    top5 = [p for p, _ in ranked[:5]]
    bottom = [p for p, _ in ranked[-3:]]
    out = {
        "timerange": TIMERANGE,
        "ranked": [{"pair": p, "pnl_usdt": round(v, 4)} for p, v in ranked],
        "top5": top5,
        "drop": bottom,
    }
    path = ROOT / "simulation/results/pair_ranking.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nTop-5: {[p.split('/')[0] for p in top5]}")
    print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
