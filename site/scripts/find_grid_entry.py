#!/usr/bin/env python3
"""Find Grid entry signal among whitelist pairs (VolatilityGridStrategy rules)."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"

ADX_MAX = 28
BB_WIDTH_MIN = 0.018
RSI_LONG = 42
RSI_SHORT = 58


def ft_to_symbol(pair: str) -> str:
    return pair.split("/")[0] + "USDT"


def fetch_klines(symbol: str, limit: int = 60) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {"category": "linear", "symbol": symbol, "interval": "5", "limit": str(limit)}
    )
    with urllib.request.urlopen(f"{BYBIT_KLINE}?{q}", timeout=30) as resp:
        data = json.loads(resp.read().decode())
    rows = list(reversed(data.get("result", {}).get("list", [])))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c])
    return df


def analyze(pair: str, df: pd.DataFrame) -> dict | None:
    if len(df) < 30:
        return None
    close = df["close"]
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    bb_width = (upper - lower) / mid.replace(0, np.nan)
    rsi = ta.RSI(df, timeperiod=14)
    adx = ta.ADX(df, timeperiod=14)

    c = float(close.iloc[-1])
    r = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50
    a = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 99
    bw = float(bb_width.iloc[-1]) if pd.notna(bb_width.iloc[-1]) else 0
    lo = float(lower.iloc[-1])
    hi = float(upper.iloc[-1])
    m = float(mid.iloc[-1])

    if a >= ADX_MAX or bw < BB_WIDTH_MIN:
        return None

    side = None
    score = 0.0
    if c <= lo and r < RSI_LONG:
        side = "long"
        score = (RSI_LONG - r) / RSI_LONG + (lo - c) / lo if lo else 0
    elif c >= hi and r > RSI_SHORT:
        side = "short"
        score = (r - RSI_SHORT) / (100 - RSI_SHORT) + (c - hi) / hi if hi else 0
    else:
        # near-band setups for watchlist
        dist_lo = (c - lo) / lo if lo else 1
        dist_hi = (hi - c) / hi if hi else 1
        if dist_lo < 0.004 and r < 48:
            side = "long_near"
            score = 0.3 * (1 - dist_lo / 0.004)
        elif dist_hi < 0.004 and r > 52:
            side = "short_near"
            score = 0.3 * (1 - dist_hi / 0.004)
        else:
            return None

    room_to_mid = abs(m - c) / c * 100
    return {
        "pair": pair,
        "side": side,
        "close": c,
        "bb_mid": m,
        "bb_lower": lo,
        "bb_upper": hi,
        "rsi": round(r, 1),
        "adx": round(a, 1),
        "bb_width_pct": round(bw * 100, 2),
        "room_to_mid_pct": round(room_to_mid, 2),
        "score": round(score, 4),
    }


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/freqtrade/freqtrade")
    ranging = json.loads((base / "user_data/ranging_pairs.json").read_text())
    whitelist = ranging.get("whitelist", [])
    hits = []
    for pair in whitelist:
        try:
            sym = ft_to_symbol(pair)
            df = fetch_klines(sym)
            row = analyze(pair, df)
            if row:
                hits.append(row)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {pair}: {exc}", file=sys.stderr)
    hits.sort(key=lambda x: (0 if x["side"] in ("long", "short") else 1, -x["score"]))
    print(json.dumps({"signals": hits, "best": hits[0] if hits else None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
