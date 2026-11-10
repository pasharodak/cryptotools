#!/usr/bin/env python3
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

"""List top Bybit USDT linear perpetuals by 24h turnover."""
import json
import urllib.request

SKIP = {"USDCUSDT", "USDEUSDT"}
# Established liquid alts (Bybit linear) — good for ML Finder / technical strategies
PREFERRED = [
    "SOL", "XRP", "DOGE", "ADA", "SUI", "NEAR", "AVAX", "LINK", "LTC", "BNB",
    "DOT", "MATIC", "POL", "ATOM", "FIL", "APT", "ARB", "OP", "INJ", "WLD",
    "ENA", "PEPE", "WIF", "BCH", "HBAR", "TON", "TRX", "UNI", "AAVE", "XLM",
]

url = "https://api.bybit.com/v5/market/tickers?category=linear"
with urllib.request.urlopen(url, timeout=30) as resp:
    data = json.loads(resp.read())

by_sym = {}
for t in data.get("result", {}).get("list", []):
    sym = t.get("symbol", "")
    if not sym.endswith("USDT") or sym in SKIP:
        continue
    by_sym[sym] = float(t.get("turnover24h") or 0)

print("Preferred pairs on Bybit (sorted by 24h volume):\n")
found = []
for base in PREFERRED:
    sym = f"{base}USDT"
    if sym in by_sym:
        found.append((by_sym[sym], f"{base}/USDT:USDT"))
found.sort(reverse=True)
for vol, pair in found:
    print(f"{vol/1e6:8.1f}M  {pair}")

print("\nSuggested top-10 for ML Finder (excl BTC/ETH in whitelist — corr pairs):")
for vol, pair in found[:10]:
    print(f"  {pair}")
