#!/usr/bin/env python3
"""Fetch top-N Bybit USDT linear futures (prod VolumePairList) and save for sim download."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_PATH = ROOT / "simulation/config/prod_pairs_200.json"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
SKIP_SYMBOLS = {"USDCUSDT", "USDEUSDT", "USD1USDT"}
MIN_TURNOVER = 1_000_000.0


def load_grid_blacklist(root: Path) -> set[str]:
    path = root / "user_data/config_grid.json"
    if not path.is_file():
        return set()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return set(cfg.get("exchange", {}).get("pair_blacklist") or [])


def symbol_to_ft_pair(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"{base}/USDT:USDT"


def fetch_top_pairs(
    root: Path,
    *,
    number_assets: int = 200,
    min_turnover: float = MIN_TURNOVER,
) -> dict[str, Any]:
    blacklist = load_grid_blacklist(root)
    with urllib.request.urlopen(BYBIT_TICKERS, timeout=45) as resp:
        data = json.loads(resp.read())

    ranked: list[tuple[float, str, str]] = []
    for t in data.get("result", {}).get("list", []):
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT") or sym in SKIP_SYMBOLS:
            continue
        pair = symbol_to_ft_pair(sym)
        if pair in blacklist:
            continue
        turnover = float(t.get("turnover24h") or 0)
        if turnover < min_turnover:
            continue
        ranked.append((turnover, sym, pair))

    ranked.sort(reverse=True)
    top = ranked[:number_assets]
    pairs = [p for _, _, p in top]

    payload = {
        "fetched_at": datetime.now(tz=UTC).isoformat(),
        "source": "bybit_linear_volume",
        "number_assets": number_assets,
        "min_turnover_usdt": min_turnover,
        "blacklist_excluded": sorted(blacklist),
        "pairs": pairs,
        "details": [
            {"pair": p, "symbol": s, "turnover24h": round(v, 2)}
            for v, s, p in top
        ],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Fetch prod top-200 Bybit USDT futures pairs")
    ap.add_argument("-n", "--number", type=int, default=200)
    ap.add_argument("--min-turnover", type=float, default=MIN_TURNOVER)
    args = ap.parse_args()

    payload = fetch_top_pairs(ROOT, number_assets=args.number, min_turnover=args.min_turnover)
    print(f"Saved {len(payload['pairs'])} pairs -> {OUT_PATH}")
    print(f"  top: {payload['pairs'][:5]}")
    print(f"  tail: {payload['pairs'][-3:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
