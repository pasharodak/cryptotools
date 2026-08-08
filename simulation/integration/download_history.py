#!/usr/bin/env python3
"""Download historical OHLCV for replay pairs via ctengine download-data."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest() -> dict:
    return json.loads((root() / "simulation" / "config" / "manifest.json").read_text(encoding="utf-8"))


def load_pairs(export_path: Path) -> list[str]:
    if not export_path.is_file():
        raise FileNotFoundError(f"Run export_trades first: {export_path}")
    data = json.loads(export_path.read_text(encoding="utf-8"))
    return data.get("all_pairs") or []


def patch_config_whitelist(config_path: Path, pairs: list[str], out_path: Path) -> None:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = pairs
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=12)
    args = parser.parse_args()

    cfg = load_manifest()
    r = root()
    export_path = args.export or (r / "simulation" / "data" / "live_trades_export.json")
    pairs = load_pairs(export_path)
    if not pairs:
        print("No pairs in export — add SQLite files to simulation/data/live_dbs/")
        return 1

    config = args.config or (r / "simulation" / "config" / "download_only.json")
    datadir = r / cfg["ctengine_datadir"]
    datadir.mkdir(parents=True, exist_ok=True)
    runtime_cfg = r / "simulation" / "data" / "runtime" / "download_batch.json"

    timerange = cfg.get("timerange", "20260618-20260626")
    ft = r / ".venv" / "Scripts" / "ctbot.exe"
    if not ft.is_file():
        ft = Path("ctengine")

    batch_size = max(1, args.batch_size)
    batches = [pairs[i : i + batch_size] for i in range(0, len(pairs), batch_size)]
    print(f"Downloading {len(pairs)} pairs in {len(batches)} batches, timerange {timerange}")

    rc = 0
    for i, batch in enumerate(batches, 1):
        patch_config_whitelist(config, batch, runtime_cfg)
        cmd = [
            str(ft),
            "download-data",
            "--config",
            str(runtime_cfg),
            "--datadir",
            str(datadir),
            "--timeframe",
            cfg.get("timeframe", "5m"),
            "--timerange",
            timerange,
            "--trading-mode",
            cfg.get("trading_mode", "futures"),
        ]
        print(f"Batch {i}/{len(batches)}: {len(batch)} pairs")
        proc = subprocess.run(cmd, cwd=str(r))
        if proc.returncode != 0:
            rc = proc.returncode
            print(f"Batch {i} failed with code {proc.returncode}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
