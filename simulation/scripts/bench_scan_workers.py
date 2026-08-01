#!/usr/bin/env python3
"""Quick benchmark for parallel scan replay."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.exchange_sim.scan_replay import build_arm_schedules  # noqa: E402
from simulation.scripts.export_grid_dataset import timerange_to_ms  # noqa: E402


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--timerange", default="20250601-20250603")
    ap.add_argument("--pairs", type=int, default=30, help="First N prod pairs")
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 4])
    args = ap.parse_args()

    pairs = list(
        json.loads((ROOT / "simulation/config/prod_pairs_200.json").read_text(encoding="utf-8")).get("pairs") or []
    )[: args.pairs]
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine", exchange="bybit")
    start_ms, end_ms = timerange_to_ms(args.timerange)

    for w in args.workers:
        t0 = time.perf_counter()
        build_arm_schedules(
            ds,
            pairs,
            start_ms,
            end_ms,
            ROOT,
            set(),
            scan_interval_ms=10 * 60 * 1000,
            workers=w,
        )
        print(f"workers={w}: {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
