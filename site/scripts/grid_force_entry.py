#!/usr/bin/env python3
"""Scan for Grid entry + optional force-enter via bot API."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
GRID_API = "http://127.0.0.1:8082/api/v1"

ADX_MAX = 28
BB_WIDTH_MIN = 0.018
RSI_LONG = 42
RSI_SHORT = 58


def http_get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_token(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    data = http_post(f"{GRID_API}/token/login", {}, {"Authorization": f"Basic {token}"})
    return data["access_token"]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch_klines(symbol: str) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {"category": "linear", "symbol": symbol, "interval": "5", "limit": "60"}
    )
    data = http_get(f"{BYBIT_KLINE}?{q}")
    rows = list(reversed(data.get("result", {}).get("list", [])))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turn"])
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col])
    return df


def check_signal(pair: str, df: pd.DataFrame) -> dict | None:
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
    r = float(rsi.iloc[-1])
    a = float(adx.iloc[-1])
    bw = float(bb_width.iloc[-1])
    lo = float(lower.iloc[-1])
    hi = float(upper.iloc[-1])
    m = float(mid.iloc[-1])

    if a >= ADX_MAX or bw < BB_WIDTH_MIN:
        return None

    side = None
    score = 0.0
    if c <= lo and r < RSI_LONG:
        side = "long"
        score = (RSI_LONG - r) / RSI_LONG + max(0, (lo - c) / lo)
    elif c >= hi and r > RSI_SHORT:
        side = "short"
        score = (r - RSI_SHORT) / (100 - RSI_SHORT) + max(0, (c - hi) / hi)

    if not side:
        return None

    return {
        "pair": pair,
        "side": side,
        "rsi": round(r, 1),
        "adx": round(a, 1),
        "bb_width_pct": round(bw * 100, 2),
        "room_to_mid_pct": round(abs(m - c) / c * 100, 2),
        "score": round(score, 4),
    }


def candidate_pairs(base: Path) -> list[str]:
    pairs: list[str] = []
    ranging_file = base / "user_data/ranging_pairs.json"
    if ranging_file.is_file():
        data = json.loads(ranging_file.read_text(encoding="utf-8"))
        pairs.extend(data.get("whitelist", []))

    tickers = http_get(BYBIT_TICKERS).get("result", {}).get("list", [])
    vols: list[tuple[float, str]] = []
    exclude = {
        "USDCUSDT",
        "USDEUSDT",
        "HUSDT",
        "FOLKSUSDT",
        "ESPORTSUSDT",
        "FARTCOINUSDT",
        "BEATUSDT",
        "GRAMUSDT",
        "BSBUSDT",
        "UBUSDT",
        "BLESSUSDT",
    }
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in exclude:
            continue
        vol = float(t.get("turnover24h") or 0)
        if vol >= 3_000_000:
            vols.append((vol, sym))
    vols.sort(reverse=True)
    for _, sym in vols[:150]:
        base_coin = sym[: -len("USDT")]
        pairs.append(f"{base_coin}/USDT:USDT")
    return list(dict.fromkeys(pairs))


def enable_force_entry(base: Path) -> None:
    cfg_path = base / "user_data/config_grid.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not cfg.get("force_entry_enable"):
        cfg["force_entry_enable"] = True
        tmp = cfg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
        tmp.replace(cfg_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enter", action="store_true", help="Force-enter best signal")
    parser.add_argument("--wait", type=int, default=0, help="Poll every N sec until signal")
    args = parser.parse_args()

    base = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
    env = load_env(Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env")))
    user = os.environ.get("FREQUI_USERNAME") or env.get("FREQUI_USERNAME", "cryptotools")
    password = os.environ.get("FREQUI_PASSWORD") or env.get("FREQUI_PASSWORD", "")
    if not password:
        print("ERROR: no API password", file=sys.stderr)
        return 1

    def scan_once() -> list[dict]:
        hits: list[dict] = []
        for i, pair in enumerate(candidate_pairs(base)):
            sym = pair.split("/")[0] + "USDT"
            try:
                df = fetch_klines(sym)
                row = check_signal(pair, df)
                if row:
                    hits.append(row)
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
                pass
            if i % 10 == 9:
                time.sleep(0.2)
        hits.sort(key=lambda x: -x["score"])
        return hits

    deadline = time.time() + args.wait if args.wait else time.time()
    best = None
    while True:
        hits = scan_once()
        if hits:
            best = hits[0]
            print(json.dumps({"found": len(hits), "best": best, "top5": hits[:5]}, indent=2))
            break
        if time.time() >= deadline:
            print(json.dumps({"found": 0, "best": None}))
            return 2
        print("no signal, waiting 60s...", file=sys.stderr)
        time.sleep(60)

    if not args.enter:
        return 0

    enable_force_entry(base)
    token = api_token(user, password)
    hdrs = {"Authorization": f"Bearer {token}"}
    http_post(f"{GRID_API}/reload_config", {}, hdrs)
    time.sleep(3)

    side = best["side"]
    payload = {"pair": best["pair"], "side": side}
    try:
        result = http_post(f"{GRID_API}/forceenter", payload, hdrs)
        print(json.dumps({"entered": result, "pair": best["pair"], "side": side}, indent=2))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(f"forceenter failed: {exc.code} {body}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
