#!/usr/bin/env python3
"""Download 1m futures OHLCV for prod200 (or all) pairs."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.paths import VENV_PYTHON  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

PY = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)


def patch_runtime(pairs: list[str], out: Path) -> None:
    cfg = json.loads((ROOT / "simulation/config/download_only.json").read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = list(pairs)
    cfg["timeframe"] = "1m"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timerange", default="20260620-20260716")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--pairs-source", default="prod200")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    pairs = pairs_from_source(ROOT, args.pairs_source)
    btc = "BTC/USDT:USDT"
    pairs = [btc] + [p for p in pairs if p != btc]
    if args.limit:
        pairs = pairs[: args.limit]

    datadir = ROOT / "simulation/data/freqtrade"
    runtime = ROOT / "simulation/data/runtime/download_1m.json"
    batches = [pairs[i : i + args.batch_size] for i in range(0, len(pairs), args.batch_size)]
    print(f"download 1m · {len(pairs)} pairs · {args.timerange} · {len(batches)} batches · py={PY}")
    rc = 0
    for i, batch in enumerate(batches, 1):
        patch_runtime(batch, runtime)
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
            "1m",
            "--timerange",
            args.timerange,
            "--trading-mode",
            "futures",
        ]
        print(f"batch {i}/{len(batches)} · {batch[0]}.. ({len(batch)})", flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT))
        if r.returncode != 0:
            rc = r.returncode
            print(f"WARN batch {i} exit {r.returncode}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
