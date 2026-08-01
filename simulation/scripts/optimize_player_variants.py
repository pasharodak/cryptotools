#!/usr/bin/env python3
"""Test player variants: scan arms + post-arm trades + combined PnL."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402
from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.exchange_sim.player_sync import (  # noqa: E402
    filter_trades_after_arm,
    resolve_armed_at,
    summarize_trades,
)
from simulation.exchange_sim.scan_replay import build_arm_schedules  # noqa: E402


def load_trades(zip_path: Path, strategy: str) -> list[dict]:
    with zipfile.ZipFile(zip_path) as zf:
        js = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(js))
    return data.get("strategy", {}).get(strategy, {}).get("trades") or []


def run_bt(config: Path, strategy: str, strategy_path: str | None, timerange: str, export: str) -> Path | None:
    ft = ROOT / ".venv" / "Scripts" / "ctbot.exe"
    out_dir = ROOT / "simulation" / "results" / "optimize_player"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(config),
        "--strategy",
        strategy,
    ]
    if strategy_path:
        cmd.extend(["--strategy-path", str(ROOT / strategy_path)])
    cmd.extend(
        [
            "--datadir",
            str(ROOT / "simulation/data/ctengine"),
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
    )
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    last = json.loads((out_dir / ".last_result.json").read_text())
    return out_dir / last["latest_backtest"]


def eval_variant(
    name: str,
    strategy: str,
    strategy_path: str | None,
    pairs: list[str],
    start_ms: int,
    end_ms: int,
    timerange: str,
) -> dict:
    runtime = ROOT / "simulation/data/runtime" / f"opt_{name}.json"
    cfg = patch_whitelist(ROOT, "simulation/config/backtest_grid_improved.json", pairs, runtime)
    bl = set(cfg.get("exchange", {}).get("pair_blacklist") or [])
    zip_path = run_bt(runtime, strategy, strategy_path, timerange, f"opt_{name}")
    if not zip_path:
        return {"name": name, "ok": False}

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    sched = build_arm_schedules(ds, pairs, start_ms, end_ms, ROOT, bl)
    raw = load_trades(zip_path, strategy)
    by_pair: dict[str, list] = defaultdict(list)
    for t in raw:
        t = dict(t)
        if "open_ms" not in t and t.get("open_timestamp"):
            t["open_ms"] = int(t["open_timestamp"])
        by_pair[t["pair"]].append(t)

    synced: dict[str, list] = {}
    armed_pairs = 0
    for pair in pairs:
        raw_p = sorted(by_pair.get(pair, []), key=lambda x: x["open_ms"])
        arm = resolve_armed_at(sched["grid_arms"].get(pair), raw_p, start_ms)
        synced[pair] = filter_trades_after_arm(raw_p, arm)
        if arm:
            armed_pairs += 1

    all_trades = [t for ts in synced.values() for t in ts]
    pairs_with_trades = sum(1 for ts in synced.values() if ts)
    summary = summarize_trades(all_trades)
    per_pair = {
        p.split("/")[0]: summarize_trades(synced[p]) for p in pairs if synced[p]
    }
    score = summary["profit_abs"] + pairs_with_trades * 0.05 + armed_pairs * 0.02
    return {
        "name": name,
        "strategy": strategy,
        "ok": True,
        "armed_pairs": armed_pairs,
        "pairs_with_trades": pairs_with_trades,
        "per_pair": per_pair,
        "score": round(score, 4),
        **summary,
    }


def main() -> int:
    manifest = json.loads((ROOT / "simulation/config/manifest.json").read_text())
    pairs = manifest["player_pairs"]
    timerange = manifest.get("player_timerange", "20260620-20260625")
    start_ms = int(datetime(2026, 6, 20, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(2026, 6, 25, tzinfo=UTC).timestamp() * 1000)

    variants = [
        ("player", "SimVolatilityGridPlayer", "simulation/strategies"),
        ("live_grid", "VolatilityGridStrategy", None),
        ("strict", "SimVolatilityGridStrategy", "simulation/strategies"),
    ]
    results = []
    for name, strat, path in variants:
        print(f"=== {name} / {strat} ===")
        r = eval_variant(name, strat, path, pairs, start_ms, end_ms, timerange)
        results.append(r)
        if not r.get("ok"):
            print("  FAILED")
            continue
        print(
            f"  armed={r['armed_pairs']} traded_pairs={r['pairs_with_trades']} "
            f"trades={r['total_trades']} pnl={r['profit_abs']:+.4f} score={r['score']}"
        )
        for sym, s in sorted(r.get("per_pair", {}).items(), key=lambda x: -x[1]["total_trades"]):
            print(f"    {sym}: {s['total_trades']} sd, {s['profit_abs']:+.4f}")

    best = max((r for r in results if r.get("ok")), key=lambda x: x["score"], default=None)
    report = {"timerange": timerange, "variants": results, "best": best["name"] if best else None}
    out = ROOT / "simulation/results/player_variants.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nBest: {report['best']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
