#!/usr/bin/env python3
"""Run grid backtest variants for player pairs and pick best net PnL."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest() -> dict:
    return json.loads((root() / "simulation" / "config" / "manifest.json").read_text(encoding="utf-8"))


def patch_config(base: Path, pairs: list[str], extra_blacklist: list[str], out: Path) -> None:
    cfg = json.loads(base.read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = pairs
    bl = list(cfg["exchange"].get("pair_blacklist") or [])
    for p in extra_blacklist:
        if p not in bl:
            bl.append(p)
    cfg["exchange"]["pair_blacklist"] = bl
    frag = root() / "simulation" / "config" / "sim_common_fragment.json"
    if frag.is_file():
        for key, val in json.loads(frag.read_text(encoding="utf-8")).items():
            if key == "exchange":
                cfg.setdefault("exchange", {}).update(val)
            else:
                cfg[key] = val
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def run_backtest(config: Path, strategy: str, timerange: str, export: str) -> dict:
    ft = root() / ".venv" / "Scripts" / "ctbot.exe"
    out_dir = root() / "simulation" / "results" / "optimize_player"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(config),
        "--strategy",
        strategy,
        "--strategy-path",
        str(root() / "simulation" / "strategies"),
        "--datadir",
        str(root() / "simulation" / "data" / "ctengine"),
        "--timerange",
        timerange,
        "--export",
        "trades",
        "--export-filename",
        export,
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    proc = subprocess.run(cmd, cwd=str(root()), capture_output=True, text=True)
    stdout = proc.stdout + proc.stderr
    m_pct = re.search(r"Total profit %.*?(-?\d+\.?\d*)", stdout)
    m_bal = re.search(r"Final balance\s*\|\s*(-?\d+\.?\d*)", stdout)
    m_tr = re.search(r"Total/Daily Avg Trades\s*\|\s*(\d+)", stdout)
    trades_pnl = 0.0
    trade_count = 0
    zips = sorted(out_dir.glob(f"{export}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
            data = json.loads(zf.read(js))
        strat_block = data.get("strategy", {}).get(strategy, {})
        trades = strat_block.get("trades") or []
        trade_count = len(trades)
        trades_pnl = sum(float(t["profit_abs"]) for t in trades)
    return {
        "ok": proc.returncode == 0,
        "profit_pct": float(m_pct.group(1)) if m_pct else None,
        "final_balance": float(m_bal.group(1)) if m_bal else None,
        "trades": int(m_tr.group(1)) if m_tr else trade_count,
        "trades_pnl": trades_pnl,
        "stdout_tail": stdout[-800:] if stdout else "",
    }


def main() -> int:
    cfg_m = load_manifest()
    pairs = cfg_m["player_pairs"]
    timerange = cfg_m.get("player_timerange", "20260620-20260625")
    runtime = root() / "simulation" / "data" / "runtime"
    base = root() / "simulation" / "config" / "backtest_grid_improved.json"

    variants = [
        ("baseline", "VolatilityGridStrategy", []),
        ("sim_grid_v1", "SimVolatilityGridStrategy", []),
        ("sim_grid_v1_no_hei", "SimVolatilityGridStrategy", ["HEI/USDT:USDT"]),
        ("sim_grid_v1_no_losers", "SimVolatilityGridStrategy", ["HEI/USDT:USDT", "NEAR/USDT:USDT"]),
    ]

    results = {}
    for name, strategy, extra_bl in variants:
        cfg_path = runtime / f"opt_{name}.json"
        patch_config(base, pairs, extra_bl, cfg_path)
        print(f"--- {name} / {strategy} blacklist+={extra_bl}")
        res = run_backtest(cfg_path, strategy, timerange, f"opt_{name}")
        results[name] = res
        print(
            f"  pct={res['profit_pct']} bal={res['final_balance']} trades={res['trades']} "
            f"pnl={res['trades_pnl']:+.4f} ok={res['ok']}"
        )

    best = max(results.items(), key=lambda x: x[1].get("trades_pnl") or -999)
    out = root() / "simulation" / "results" / "optimize_player_report.json"
    out.write_text(json.dumps({"timerange": timerange, "pairs": pairs, "results": results, "best": best[0]}, indent=2), encoding="utf-8")
    print(f"\nBest: {best[0]} pnl={best[1]['trades_pnl']:+.4f}")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
