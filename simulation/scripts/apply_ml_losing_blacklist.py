#!/usr/bin/env python3
"""Append ML-losing pairs to strategy/grid/finder pair_blacklist (local site configs)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE_UD = ROOT / "site/user_data"
BL_FILE = ROOT / "simulation/results/prod_ml_losing_blacklist.json"

CONFIGS = [
    SITE_UD / "config_strategy.json",
    SITE_UD / "config_grid.json",
    SITE_UD / "config.json",
]


def main() -> int:
    data = json.loads(BL_FILE.read_text(encoding="utf-8"))
    pairs = list(data.get("pairs") or [])
    if not pairs:
        print("no pairs", file=sys.stderr)
        return 1
    for path in CONFIGS:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        ex = cfg.setdefault("exchange", {})
        bl = list(ex.get("pair_blacklist") or [])
        wl = list(ex.get("pair_whitelist") or [])
        added = []
        for p in pairs:
            if p not in bl:
                bl.append(p)
                added.append(p)
            if p in wl:
                wl = [x for x in wl if x != p]
        ex["pair_blacklist"] = bl
        ex["pair_whitelist"] = wl
        path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
        print(f"{path.name}: +{len(added)} blacklist now={len(bl)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
