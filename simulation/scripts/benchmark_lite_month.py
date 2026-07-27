#!/usr/bin/env python3
"""Benchmark enabled strategies with per-strategy pair assignments."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402
from simulation.scripts.select_player_pairs import assign_pairs, save_assignment  # noqa: E402


def run_one(sc: dict, pairs: list[str], timerange: str) -> dict:
    sid = sc["id"]
    if not pairs:
        return {
            "id": sid,
            "label": sc["label"],
            "ok": True,
            "skipped": True,
            "pairs": 0,
            "profit_pct": 0,
            "final_balance": None,
            "trades": 0,
            "pnl_usdt": 0,
        }
    runtime = ROOT / "simulation/data/runtime" / f"bench_{sid}.json"
    patch_whitelist(ROOT, sc["config"], pairs, runtime, scenario=sc)
    ft = ROOT / ".venv/Scripts/freqtrade.exe"
    out_dir = ROOT / "simulation/results/lite_benchmark"
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
        timerange,
        "--export",
        "trades",
        "--export-filename",
        f"bench_{sid}",
        "--backtest-directory",
        str(out_dir),
        "--cache",
        "none",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    m_pct = re.search(r"Total profit %.*?(-?\d+\.?\d*)", out)
    m_bal = re.search(r"Final balance\s*\|\s*(-?\d+\.?\d*)", out)
    m_tr = re.search(r"Total/Daily Avg Trades\s*\|\s*(\d+)", out)
    pnl = 0.0
    trades_n = 0
    last_meta = out_dir / ".last_result.json"
    zip_path = None
    if last_meta.is_file():
        latest = json.loads(last_meta.read_text(encoding="utf-8")).get("latest_backtest")
        if latest:
            candidate = out_dir / latest
            if candidate.is_file():
                zip_path = candidate
    if zip_path:
        with zipfile.ZipFile(zip_path) as zf:
            js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
            data = json.loads(zf.read(js))
        block = data.get("strategy", {}).get(sc["strategy"], {})
        raw = block.get("trades") or []
        trades_n = len(raw)
        pnl = sum(float(t["profit_abs"]) for t in raw)
    return {
        "id": sid,
        "label": sc["label"],
        "ok": proc.returncode == 0,
        "pairs": len(pairs),
        "pair_list": [p.split("/")[0] for p in pairs],
        "profit_pct": float(m_pct.group(1)) if m_pct else None,
        "final_balance": float(m_bal.group(1)) if m_bal else None,
        "trades": int(m_tr.group(1)) if m_tr else trades_n,
        "pnl_usdt": round(pnl, 4),
    }


def main() -> int:
    cfg = json.loads((ROOT / "simulation/config/manifest.json").read_text(encoding="utf-8"))
    scenarios = [
        s
        for s in json.loads((ROOT / "simulation/config/player_scenarios.json").read_text(encoding="utf-8"))
        if s.get("enabled", True)
    ]
    profile = json.loads((ROOT / "simulation/config/player_profile.json").read_text(encoding="utf-8"))
    datadir = ROOT / cfg.get("freqtrade_datadir", "simulation/data/freqtrade")
    timerange = cfg.get("player_timerange", "20260601-20260625")
    start_s = timerange.split("-")[0]
    start_ms = int(datetime.strptime(start_s, "%Y%m%d").replace(tzinfo=UTC).timestamp() * 1000)

    result = assign_pairs(ROOT, datadir, start_ms, profile=profile, use_cache=False)
    from simulation.scripts.select_player_pairs import pool_pairs

    save_assignment(result, ROOT / "simulation/results/selected_player_pairs.json", start_ms, pool_pairs(ROOT))

    results = [
        run_one(sc, result.assignments.get(sc["id"], []), timerange) for sc in scenarios
    ]
    combined_pnl = sum(r["pnl_usdt"] for r in results if r.get("ok"))
    report = {
        "timerange": timerange,
        "pairs": len(result.pairs),
        "assignments": {k: [p.split("/")[0] for p in v] for k, v in result.assignments.items() if v},
        "combined_pnl": round(combined_pnl, 4),
        "strategies": results,
    }
    out = ROOT / "simulation/results/lite_month_benchmark.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
