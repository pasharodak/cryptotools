#!/usr/bin/env python3
"""Patch stoploss in freqtrade configs without touching other fields."""
import json
import sys
from pathlib import Path

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/freqtrade/freqtrade")
STOP = float(sys.argv[2]) if len(sys.argv) > 2 else -0.05

for name in ("config.json", "config_strategy.json", "config_grid.json"):
    path = BASE / "user_data" / name
    if not path.is_file():
        print(f"skip {name}")
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    old = data.get("stoploss")
    data["stoploss"] = STOP
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)
    print(f"{name}: {old} -> {STOP}")
