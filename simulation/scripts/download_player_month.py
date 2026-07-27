#!/usr/bin/env python3
"""Download 5m + 1s data for player pairs from start of month."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import patch_whitelist  # noqa: E402


def _needs_subminute(pair: str, timerange: str, datadir: Path) -> bool:
    """Return True if bybit 1s file is missing or does not cover timerange start."""
    import pandas as pd

    start_s = timerange.split("-")[0]
    want = pd.Timestamp(start_s, tz="UTC")
    sym = pair.replace("/", "_").replace(":", "_")
    fp = datadir / "bybit" / f"{sym}-1s-futures.feather"
    if not fp.is_file():
        return True
    df = pd.read_feather(fp)
    if df.empty:
        return True
    have = pd.to_datetime(df.date.iloc[0], unit="ms", utc=True)
    return have > want


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Download 5m + 1s for player pairs")
    ap.add_argument("--skip-1s", action="store_true", help="Only download 5m (faster for backfill)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "simulation/config/manifest.json").read_text(encoding="utf-8"))
    pool_path = ROOT / "simulation/config/player_pair_pool.json"
    pairs = json.loads(pool_path.read_text(encoding="utf-8")).get("pairs") or cfg.get("player_pairs") or []
    timerange = cfg.get("player_timerange", "20260601-20260625")
    datadir = ROOT / cfg.get("freqtrade_datadir", "simulation/data/freqtrade")
    runtime = ROOT / "simulation/data/runtime/download_player_month.json"
    patch_whitelist(ROOT, "simulation/config/backtest_lite_base.json", pairs, runtime)

    ft = ROOT / ".venv/Scripts/freqtrade.exe"
    futures_dir = datadir / "futures"
    missing_5m = []
    want_start = timerange.split("-")[0]
    for p in pairs:
        sym = p.replace("/", "_").replace(":", "_")
        fp = futures_dir / f"{sym}-5m-futures.feather"
        if not fp.is_file():
            missing_5m.append(p)
            continue
        import pandas as pd

        df = pd.read_feather(fp)
        if df.empty:
            missing_5m.append(p)
            continue
        if "date" in df.columns:
            have = pd.to_datetime(df["date"].iloc[0], unit="ms", utc=True)
        else:
            have = pd.to_datetime(df.index[0], utc=True)
        if have > pd.Timestamp(want_start, tz="UTC"):
            missing_5m.append(p)
    prepend_pairs = missing_5m[:]
    if missing_5m:
        print(f"=== 5m download {timerange} · {len(missing_5m)} pairs (futures/) ===")
        cmd = [
            str(ft),
            "download-data",
            "--config",
            str(runtime),
            "--datadir",
            str(datadir),
            "--timeframe",
            "5m",
            "--timerange",
            timerange,
            "--trading-mode",
            "futures",
        ]
        if prepend_pairs:
            cmd.append("--prepend")
        r1 = subprocess.run(cmd, cwd=str(ROOT))
    else:
        print("=== 5m futures/ already present — skip freqtrade download ===")
        r1 = subprocess.CompletedProcess(args=[], returncode=0)

    need_1s = [p for p in pairs if _needs_subminute(p, timerange, datadir)]
    if args.skip_1s or not need_1s:
        if args.skip_1s:
            print("=== skip 1s (--skip-1s) ===")
        else:
            print("=== 1s bybit/ already covers timerange — skip subminute ===")
        return r1.returncode

    print(f"=== 1s subminute {timerange} · {len(need_1s)} pairs (bybit/) ===")
    r2 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "simulation/integration/download_subminute.py"),
            "--timerange",
            timerange,
            "--pairs",
            *need_1s,
        ],
        cwd=str(ROOT),
    )
    return r1.returncode or r2.returncode


if __name__ == "__main__":
    raise SystemExit(main())
