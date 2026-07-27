#!/usr/bin/env python3
"""Download 5m OHLCV for all known pairs from Jan 2025 (--prepend)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402
from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.paths import VENV_PYTHON  # noqa: E402
from simulation.scripts.comparison_common import DEFAULT_TIMERANGE, update_manifest_timerange  # noqa: E402

PY = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)


def collect_pairs(root: Path, source: str = "all") -> list[str]:
    if source in ("prod200", "prod"):
        from simulation.scripts.comparison_common import pairs_from_source

        return pairs_from_source(root, "prod200")
    datadir = root / "simulation/data/freqtrade"
    ds = HistoricalDatastore(datadir)
    pairs = set(ds.list_pairs("5m"))
    export = root / "simulation/data/live_trades_export.json"
    if export.is_file():
        pairs.update(json.loads(export.read_text(encoding="utf-8")).get("all_pairs") or [])
    for extra in ("player_pair_pool.json", "extension_pairs.json", "prod_pairs_200.json"):
        path = root / "simulation/config" / extra
        if path.is_file():
            pairs.update(json.loads(path.read_text(encoding="utf-8")).get("pairs") or [])
    return sorted(pairs)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Download all pairs 5m history")
    ap.add_argument("--timerange", default=DEFAULT_TIMERANGE)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--pairs-source", choices=("all", "prod200"), default="all")
    args = ap.parse_args()

    pairs = collect_pairs(ROOT, source=args.pairs_source)
    update_manifest_timerange(ROOT, args.timerange)
    datadir = ROOT / "simulation/data/freqtrade"
    runtime = ROOT / "simulation/data/runtime/download_all_pairs.json"

    print(f"=== download {len(pairs)} pairs · {args.timerange} ===")
    batches = [pairs[i : i + args.batch_size] for i in range(0, len(pairs), args.batch_size)]
    rc = 0
    for i, batch in enumerate(batches, 1):
        patch_whitelist(ROOT, "simulation/config/download_only.json", batch, runtime)
        cmd = [
            str(PY),
            "-m",
            "freqtrade",
            "download-data",
            "--config",
            str(runtime),
            "--datadir",
            str(datadir),
            "--timeframe",
            "5m",
            "--timerange",
            args.timerange,
            "--trading-mode",
            "futures",
            "--prepend",
        ]
        print(f"batch {i}/{len(batches)} · {len(batch)} pairs", flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT))
        if r.returncode != 0:
            rc = r.returncode
            print(f"WARN batch {i} exit {r.returncode}")
    print(f"done · {len(pairs)} pairs")
    return 0 if rc == 0 else 0  # continue pipeline even if some batches failed (data may already exist)


if __name__ == "__main__":
    raise SystemExit(main())
