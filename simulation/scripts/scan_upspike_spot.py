#!/usr/bin/env python3
"""Scan Bybit spot USDT pairs for frequent upward 5m spikes + uptrend.

«Прострел вверх» = бар, где high заметно выстреливает выше тела
относительно ATR / среднего диапазона (длинный верхний фитиль или
импульсный хай поверх недавнего максимума).

Usage:
  python simulation/scripts/scan_upspike_spot.py
  python simulation/scripts/scan_upspike_spot.py --top 40 --min-turnover 2e6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=spot"
BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"


def http_get(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "cryptotools-scan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_spot_usdt_tickers(*, min_turnover: float) -> list[dict[str, Any]]:
    data = http_get(BYBIT_TICKERS)
    rows = data.get("result", {}).get("list", []) or []
    out: list[dict[str, Any]] = []
    for t in rows:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        # skip leveraged tokens / weird suffixes common on spot
        base = sym[: -len("USDT")]
        if any(x in base for x in ("UP", "DOWN", "3L", "3S", "5L", "5S")):
            continue
        try:
            turn = float(t.get("turnover24h") or 0)
        except (TypeError, ValueError):
            turn = 0.0
        if turn < min_turnover:
            continue
        try:
            last = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last <= 0:
            continue
        out.append({"symbol": sym, "turnover24h": turn, "lastPrice": last})
    out.sort(key=lambda x: x["turnover24h"], reverse=True)
    return out


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {
            "category": "spot",
            "symbol": symbol,
            "interval": interval,
            "limit": str(limit),
        }
    )
    data = http_get(f"{BYBIT_KLINE}?{q}")
    rows = data.get("result", {}).get("list", []) or []
    if not rows:
        return pd.DataFrame()
    rows = list(reversed(rows))
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    )
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    return df.reset_index(drop=True)


def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def analyze_pair(
    df: pd.DataFrame,
    *,
    wick_atr_mult: float,
    wick_range_share: float,
    lookback_high: int,
    min_spike_share: float,
    min_trend_ret: float,
) -> dict[str, float] | None:
    """Return metrics if pair has frequent up-spikes and uptrend on 5m."""
    if len(df) < 80:
        return None

    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    body_top = pd.concat([o, c], axis=1).max(axis=1)
    upper_wick = (h - body_top).clip(lower=0)
    full_range = (h - l).replace(0, np.nan)
    atr = _wilder_atr(df, 14)
    atr_prev = atr.shift(1)

    # Spike type A: long upper wick vs ATR / bar range
    wick_vs_atr = upper_wick / atr_prev.replace(0, np.nan)
    wick_vs_range = upper_wick / full_range
    spike_wick = (wick_vs_atr >= wick_atr_mult) | (wick_vs_range >= wick_range_share)

    # Spike type B: high pierces above recent max high (excluding current bar)
    prior_max = h.shift(1).rolling(lookback_high, min_periods=max(5, lookback_high // 2)).max()
    pierce = h > prior_max * 1.002  # >0.2% above prior swing high
    pierce_wick = pierce & (upper_wick / atr_prev.replace(0, np.nan) >= 0.6)

    is_spike = (spike_wick | pierce_wick).fillna(False)
    # ignore warmup
    valid = atr_prev.notna() & (atr_prev > 0)
    spikes = is_spike & valid
    n_valid = int(valid.sum())
    if n_valid < 60:
        return None

    spike_count = int(spikes.sum())
    spike_share = spike_count / n_valid
    if spike_share < min_spike_share:
        return None

    # Uptrend filters (last ~1 day of 5m ≈ 288 bars; use available tail)
    tail_n = min(len(df), 288)
    tail = df.iloc[-tail_n:]
    close_tail = tail["close"].astype(float)
    ema20 = close_tail.ewm(span=20, adjust=False).mean()
    ema50 = close_tail.ewm(span=50, adjust=False).mean()
    ema20_last = float(ema20.iloc[-1])
    ema50_last = float(ema50.iloc[-1])
    close_last = float(close_tail.iloc[-1])
    close_first = float(close_tail.iloc[0])
    trend_ret = (close_last / close_first) - 1.0 if close_first > 0 else 0.0

    # slope of ema50 over last ~6h (72 bars)
    slope_n = min(72, len(ema50) - 1)
    if slope_n < 20:
        return None
    ema50_prev = float(ema50.iloc[-1 - slope_n])
    ema50_slope = (ema50_last / ema50_prev) - 1.0 if ema50_prev > 0 else 0.0

    above_ema = float((close_tail > ema50).tail(96).mean()) if len(close_tail) >= 50 else 0.0
    bull_stack = close_last > ema20_last > ema50_last

    if trend_ret < min_trend_ret:
        return None
    if ema50_slope < 0:
        return None
    if not bull_stack and above_ema < 0.55:
        return None

    # average spike size (upper wick / ATR)
    spike_sizes = wick_vs_atr[spikes].dropna()
    avg_spike_atr = float(spike_sizes.mean()) if len(spike_sizes) else 0.0
    pierce_share = float((pierce_wick & valid).sum() / n_valid)

    # score: frequent + large spikes, stronger uptrend
    score = (
        100.0 * spike_share
        + 15.0 * avg_spike_atr
        + 40.0 * pierce_share
        + 80.0 * max(trend_ret, 0.0)
        + 60.0 * max(ema50_slope, 0.0)
        + 10.0 * above_ema
    )

    hours = n_valid * 5 / 60.0
    return {
        "spike_count": float(spike_count),
        "spike_share": spike_share,
        "spikes_per_day": spike_count / max(hours / 24.0, 1e-6),
        "avg_spike_atr": avg_spike_atr,
        "pierce_share": pierce_share,
        "trend_ret": trend_ret,
        "ema50_slope": ema50_slope,
        "above_ema50": above_ema,
        "bull_stack": 1.0 if bull_stack else 0.0,
        "bars": float(n_valid),
        "score": score,
    }


def scan_symbol(sym_row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    symbol = sym_row["symbol"]
    try:
        df = fetch_klines(symbol, args.interval, args.limit)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None
    if df.empty:
        return None
    m = analyze_pair(
        df,
        wick_atr_mult=args.wick_atr,
        wick_range_share=args.wick_range,
        lookback_high=args.lookback_high,
        min_spike_share=args.min_spike_share,
        min_trend_ret=args.min_trend_ret,
    )
    if not m:
        return None
    base = symbol[: -len("USDT")]
    return {
        "symbol": symbol,
        "pair": f"{base}/USDT",
        "turnover24h": sym_row["turnover24h"],
        **m,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Scan Bybit spot for 5m up-spikes + uptrend")
    p.add_argument("--interval", default="5", help="Bybit kline interval (default 5 = 5m)")
    p.add_argument("--limit", type=int, default=200, help="Klines per pair (max 1000)")
    p.add_argument("--min-turnover", type=float, default=1_000_000.0, help="Min 24h USDT turnover")
    p.add_argument("--top-liquidity", type=int, default=180, help="Scan top-N by turnover")
    p.add_argument("--top", type=int, default=30, help="Print top-N results")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--wick-atr", type=float, default=1.2, help="Upper wick >= N * ATR")
    p.add_argument("--wick-range", type=float, default=0.55, help="Upper wick / bar range")
    p.add_argument("--lookback-high", type=int, default=12, help="Bars for prior swing high")
    p.add_argument(
        "--min-spike-share",
        type=float,
        default=0.08,
        help="Min share of bars that are up-spikes",
    )
    p.add_argument(
        "--min-trend-ret",
        type=float,
        default=0.02,
        help="Min return over lookback window (default +2%)",
    )
    p.add_argument("--json-out", type=str, default="", help="Optional path to write JSON")
    args = p.parse_args()

    print(
        f"Fetching Bybit spot tickers (min turnover {args.min_turnover:,.0f} USDT)...",
        flush=True,
    )
    tickers = fetch_spot_usdt_tickers(min_turnover=args.min_turnover)
    tickers = tickers[: max(1, args.top_liquidity)]
    print(f"Scanning {len(tickers)} pairs on {args.interval}m x {args.limit} bars...", flush=True)

    results: list[dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(scan_symbol, row, args): row["symbol"] for row in tickers}
        for fut in as_completed(futs):
            done += 1
            if done % 25 == 0 or done == len(futs):
                print(f"  progress {done}/{len(futs)}", flush=True)
            try:
                row = fut.result()
            except Exception:
                continue
            if row:
                results.append(row)
            time.sleep(0)  # yield

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[: max(1, args.top)]

    if not top:
        print("No pairs matched filters. Try lowering --min-spike-share / --min-trend-ret.")
        return 1

    print()
    print(
        f"{'#':>3}  {'pair':<14}  {'spikes/d':>8}  {'spike%':>7}  "
        f"{'trend%':>7}  {'ema50sl%':>8}  {'score':>7}  {'turn24h':>12}"
    )
    print("-" * 90)
    for i, r in enumerate(top, 1):
        print(
            f"{i:>3}  {r['pair']:<14}  {r['spikes_per_day']:8.1f}  {100 * r['spike_share']:6.1f}%  "
            f"{100 * r['trend_ret']:6.1f}%  {100 * r['ema50_slope']:7.2f}%  "
            f"{r['score']:7.2f}  {r['turnover24h']:12,.0f}"
        )

    print()
    print("Pairs (spot):")
    for r in top:
        print(r["pair"])

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"generated": time.time(), "results": top}, f, indent=2)
        print(f"\nWrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
