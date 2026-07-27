#!/usr/bin/env python3
"""Per-pair grid backtest across full player pool — portability smoke test."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402

TIMERANGE = "20260601-20260625"
STRATEGY = "SimVolatilityGridAggressive"
PROBE_STAKE = 50


def grid_pnl(pair: str, stake: int = PROBE_STAKE) -> tuple[float, int]:
    sc = {
        "id": "pool_test",
        "config": "simulation/config/backtest_grid_improved.json",
        "strategy_path": "simulation/strategies",
        "stake_usdt": stake,
    }
    safe = pair.replace("/", "_").replace(":", "_")
    runtime = ROOT / "simulation/data/runtime" / f"pool_test_{safe}.json"
    patch_whitelist(ROOT, sc["config"], [pair], runtime, scenario=sc)
    out_dir = ROOT / "simulation/results/pool_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ROOT / ".venv/Scripts/freqtrade.exe"),
        "backtesting",
        "--config",
        str(runtime),
        "--strategy",
        STRATEGY,
        "--strategy-path",
        str(ROOT / "simulation/strategies"),
        "--datadir",
        str(ROOT / "simulation/data/freqtrade"),
        "--timerange",
        TIMERANGE,
        "--export",
        "trades",
        "--export-filename",
        f"pool_{pair.split('/')[0]}",
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return float("nan"), 0
    last = out_dir / ".last_result.json"
    if not last.is_file():
        return 0.0, 0
    latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest")
    zpath = out_dir / latest
    if not zpath.is_file():
        return 0.0, 0
    with zipfile.ZipFile(zpath) as zf:
        js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(js))
    trades = data.get("strategy", {}).get(STRATEGY, {}).get("trades") or []
    return sum(float(t["profit_abs"]) for t in trades), len(trades)


def main() -> int:
    pool = json.loads((ROOT / "simulation/config/player_pair_pool.json").read_text(encoding="utf-8"))["pairs"]
    results: list[dict] = []
    print(f"=== Grid test · {len(pool)} pairs · stake {PROBE_STAKE} · {TIMERANGE} ===\n")
    for pair in pool:
        sym = pair.split("/")[0]
        print(f"  {sym}…", flush=True)
        pnl, trades = grid_pnl(pair)
        row = {"pair": pair, "symbol": sym, "pnl_usdt": round(pnl, 4) if pnl == pnl else None, "trades": trades}
        results.append(row)
        pnl_s = f"{pnl:+.4f}" if pnl == pnl else "ERR"
        print(f"    {pnl_s} USDT · {trades} trades")

    ranked = sorted(
        [r for r in results if r["pnl_usdt"] is not None],
        key=lambda x: x["pnl_usdt"],
        reverse=True,
    )
    positive = [r for r in ranked if r["pnl_usdt"] > 0]
    print(f"\n--- Summary ---")
    print(f"  tested: {len(results)} | positive: {len(positive)} | negative: {len(ranked) - len(positive)}")
    print(f"  top-3: {', '.join(r['symbol'] for r in ranked[:3])}")
    if positive:
        print(f"  best: {ranked[0]['symbol']} {ranked[0]['pnl_usdt']:+.4f} USDT")

    out = {
        "timerange": TIMERANGE,
        "strategy": STRATEGY,
        "probe_stake": PROBE_STAKE,
        "pairs_tested": len(results),
        "positive": len(positive),
        "ranked": ranked,
    }
    path = ROOT / "simulation/results/pool_grid_test.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
