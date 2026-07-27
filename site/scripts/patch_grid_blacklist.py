#!/usr/bin/env python3
import json
import sys
from pathlib import Path

pairs = sys.argv[1:] if len(sys.argv) > 1 else ["SKHYNIX/USDT:USDT"]
p = Path("/home/freqtrade/freqtrade/user_data/config_grid.json")
cfg = json.loads(p.read_text())
ex = cfg.setdefault("exchange", {})
bl = ex.setdefault("pair_blacklist", [])
for pair in pairs:
    if pair not in bl:
        bl.append(pair)
    ex["pair_whitelist"] = [x for x in ex.get("pair_whitelist", []) if x != pair]
p.write_text(json.dumps(cfg, indent=4) + "\n")
print("whitelist:", ex["pair_whitelist"])
print("blacklist:", ex["pair_blacklist"])
