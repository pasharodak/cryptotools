#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from simulation.exchange_sim.bot_session import patch_whitelist
from simulation.exchange_sim.datastore import HistoricalDatastore
from simulation.exchange_sim.scan_replay import build_arm_schedules


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root / "simulation/config/manifest.json").read_text())
    pairs = cfg["player_pairs"]
    start_ms = int(datetime(2026, 6, 20, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(2026, 6, 25, tzinfo=UTC).timestamp() * 1000)
    ds = HistoricalDatastore(root / "simulation/data/ctengine", exchange="bybit")
    grid_cfg = patch_whitelist(
        root,
        "simulation/config/backtest_grid_improved.json",
        pairs,
        root / "simulation/data/runtime/_tmp_scan.json",
    )
    bl = set(grid_cfg.get("exchange", {}).get("pair_blacklist") or [])
    sched = build_arm_schedules(ds, pairs, start_ms, end_ms, root, bl)

    def fmt(ms: int | None) -> str:
        if not ms:
            return "NEVER"
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%m-%d %H:%M")

    print("=== Grid scanner arms ===")
    for p in pairs:
        print(f"  {p.split('/')[0]:6} {fmt(sched['grid_arms'].get(p))}")
    print("\n=== Strategy scanner arms ===")
    for p in pairs:
        print(f"  {p.split('/')[0]:6} {fmt(sched['strategy_arms'].get(p))}")
    print(f"\nScan events: {len(sched.get('events') or [])}")
    for (sid, sym), n in sorted(Counter((e["scenario_id"], e["pair"].split("/")[0]) for e in sched.get("events") or []).items()):
        print(f"  {sid} -> {sym}: {n}x")


if __name__ == "__main__":
    main()
