#!/usr/bin/env python3
"""Scan Bybit spot/linear for «ёрш» charts: jagged bilateral spikes, low trend efficiency.

Ёрш = frequent upper + lower wicks / pierces, high path vs net move,
ATR alive, not a clean trend candle.

Usage:
  python simulation/scripts/scan_ersh_spot.py
  python simulation/scripts/scan_ersh_spot.py --category linear --min-atr-pct 0.006
  python simulation/scripts/scan_ersh_spot.py --category both --top 40
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

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
STABLES = {"USDC", "USD1", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD", "EUR"}


def http_get(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "cryptotools-scan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_usdt_tickers(category: str, *, min_turnover: float) -> list[dict[str, Any]]:
    data = http_get(f"https://api.bybit.com/v5/market/tickers?category={category}")
    out: list[dict[str, Any]] = []
    for t in data.get("result", {}).get("list", []) or []:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        base = sym[: -len("USDT")]
        if any(x in base for x in ("UP", "DOWN", "3L", "3S", "5L", "5S")):
            continue
        if base in STABLES:
            continue
        try:
            turn = float(t.get("turnover24h") or 0)
            last = float(t.get("lastPrice") or 0)
            chg = float(t.get("price24hPcnt") or 0)
        except (TypeError, ValueError):
            continue
        if turn < min_turnover or last <= 0:
            continue
        out.append(
            {
                "symbol": sym,
                "category": category,
                "turnover24h": turn,
                "lastPrice": last,
                "chg24h": chg,
            }
        )
    out.sort(key=lambda x: x["turnover24h"], reverse=True)
    return out


def fetch_klines(symbol: str, category: str, interval: str, limit: int) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {"category": category, "symbol": symbol, "interval": interval, "limit": str(limit)}
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


def analyze_ersh(
    df: pd.DataFrame,
    *,
    wick_atr: float,
    wick_range: float,
    min_up_share: float,
    min_dn_share: float,
    max_efficiency: float,
    min_atr_pct: float,
    max_abs_trend: float,
) -> dict[str, float] | None:
    if len(df) < 100:
        return None

    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bot = pd.concat([o, c], axis=1).min(axis=1)
    upper = (h - body_top).clip(lower=0)
    lower = (body_bot - l).clip(lower=0)
    full = (h - l).replace(0, np.nan)
    atr = _wilder_atr(df, 14)
    atr_prev = atr.shift(1).replace(0, np.nan)

    up_flag = ((upper / atr_prev >= wick_atr) | (upper / full >= wick_range)).fillna(False)
    dn_flag = ((lower / atr_prev >= wick_atr) | (lower / full >= wick_range)).fillna(False)

    # both-side "spine" bar (classic ёрш tooth)
    both_flag = up_flag & dn_flag

    valid = atr_prev.notna()
    n = int(valid.sum())
    if n < 80:
        return None

    up_share = float((up_flag & valid).sum() / n)
    dn_share = float((dn_flag & valid).sum() / n)
    both_share = float((both_flag & valid).sum() / n)
    if up_share < min_up_share or dn_share < min_dn_share:
        return None

    # balance: ёрш needs BOTH sides, not one-way spikes
    balance = min(up_share, dn_share) / max(up_share, dn_share)

    net = abs(float(c.iloc[-1] - c.iloc[0]))
    path = float(c.diff().abs().sum())
    efficiency = net / path if path > 1e-12 else 1.0
    if efficiency > max_efficiency:
        return None

    px = float(c.iloc[-1])
    atr_last = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    atr_pct = atr_last / px if px > 0 else 0.0
    if atr_pct < min_atr_pct:
        return None

    trend_ret = float(c.iloc[-1] / c.iloc[0] - 1.0) if float(c.iloc[0]) > 0 else 0.0
    if abs(trend_ret) > max_abs_trend:
        # strong one-way move ≠ ёрш (more like impulse / dump)
        return None

    hi, lo = float(h.max()), float(l.min())
    range_pct = (hi - lo) / px if px > 0 else 0.0

    ret = c.pct_change().dropna()
    ret_std = float(ret.std()) if len(ret) else 0.0
    ac1 = float(ret.autocorr(lag=1) or 0.0) if len(ret) > 30 else 0.0
    if not np.isfinite(ac1):
        ac1 = 0.0

    # EMA crosses (sawtooth crosses midline often)
    ema20 = c.ewm(span=20, adjust=False).mean()
    above = c > ema20
    crosses = float((above != above.shift(1)).fillna(False).sum())
    hours = n * 5 / 60.0
    crosses_per_day = crosses / max(hours / 24.0, 1e-6)

    hours = max(hours, 1e-6)
    up_per_day = float((up_flag & valid).sum()) / (hours / 24.0)
    dn_per_day = float((dn_flag & valid).sum()) / (hours / 24.0)

    # score: bilateral spikes + chop + ATR + balance + crosses; penalize trend & positive AC
    score = (
        120.0 * (up_share + dn_share)
        + 80.0 * both_share
        + 40.0 * balance
        + 50.0 * min(atr_pct / 0.01, 2.0)
        + 30.0 * min(range_pct / 0.25, 2.0)
        + 0.4 * crosses_per_day
        + 25.0 * max(0.0, -ac1)  # mean-revert helps
        - 40.0 * efficiency
        - 25.0 * abs(trend_ret)
    )

    return {
        "up_share": up_share,
        "dn_share": dn_share,
        "both_share": both_share,
        "balance": balance,
        "up_per_day": up_per_day,
        "dn_per_day": dn_per_day,
        "efficiency": efficiency,
        "atr_pct": atr_pct,
        "range_pct": range_pct,
        "trend_ret": trend_ret,
        "ret_std": ret_std,
        "ac1": ac1,
        "crosses_per_day": crosses_per_day,
        "bars": float(n),
        "score": score,
    }


def scan_one(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    cat = row.get("category", "spot")
    try:
        df = fetch_klines(row["symbol"], cat, args.interval, args.limit)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None
    if df.empty:
        return None
    m = analyze_ersh(
        df,
        wick_atr=args.wick_atr,
        wick_range=args.wick_range,
        min_up_share=args.min_up_share,
        min_dn_share=args.min_dn_share,
        max_efficiency=args.max_efficiency,
        min_atr_pct=args.min_atr_pct,
        max_abs_trend=args.max_abs_trend,
    )
    if not m:
        return None
    base = row["symbol"][: -len("USDT")]
    pair = f"{base}/USDT:USDT" if cat == "linear" else f"{base}/USDT"
    return {
        "symbol": row["symbol"],
        "category": cat,
        "pair": pair,
        "turnover24h": row["turnover24h"],
        "chg24h": row["chg24h"],
        **m,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Scan Bybit spot/linear for ёрш")
    p.add_argument("--category", choices=["spot", "linear", "both"], default="spot")
    p.add_argument("--interval", default="5")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--min-turnover", type=float, default=400_000.0)
    p.add_argument("--top-liquidity", type=int, default=220)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--wick-atr", type=float, default=1.0)
    p.add_argument("--wick-range", type=float, default=0.50)
    p.add_argument("--min-up-share", type=float, default=0.08)
    p.add_argument("--min-dn-share", type=float, default=0.08)
    p.add_argument("--max-efficiency", type=float, default=0.06)
    p.add_argument("--min-atr-pct", type=float, default=0.0025)  # 0.25%
    p.add_argument("--max-abs-trend", type=float, default=0.18)  # |net| over window
    p.add_argument("--sort", choices=["score", "atr"], default="score")
    p.add_argument("--json-out", default="")
    args = p.parse_args()

    cats = ["spot", "linear"] if args.category == "both" else [args.category]
    tickers: list[dict[str, Any]] = []
    for cat in cats:
        print(
            f"Fetching {cat} tickers (min turnover {args.min_turnover:,.0f})...",
            flush=True,
        )
        rows = fetch_usdt_tickers(cat, min_turnover=args.min_turnover)[: args.top_liquidity]
        tickers.extend(rows)
    print(f"Scanning {len(tickers)} markets for ersh on {args.interval}m...", flush=True)

    results: list[dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(scan_one, row, args): row["symbol"] for row in tickers}
        for fut in as_completed(futs):
            done += 1
            if done % 25 == 0 or done == len(futs):
                print(f"  progress {done}/{len(futs)}", flush=True)
            try:
                r = fut.result()
            except Exception:
                continue
            if r:
                results.append(r)

    if args.sort == "atr":
        results.sort(key=lambda x: (x["atr_pct"], x["score"]), reverse=True)
    else:
        results.sort(key=lambda x: x["score"], reverse=True)
    top = results[: max(1, args.top)]
    if not top:
        print("No ersh pairs matched. Loosen --min-up-share / --min-atr-pct.")
        return 1

    print()
    print(
        f"{'#':>3}  {'pair':<18}  {'mkt':>6}  {'score':>6}  {'up%':>5}  {'dn%':>5}  "
        f"{'bal':>4}  {'ATR%':>6}  {'eff':>5}  {'trend%':>7}  {'turn24h':>12}"
    )
    print("-" * 115)
    for i, r in enumerate(top, 1):
        print(
            f"{i:>3}  {r['pair']:<18}  {r['category']:>6}  {r['score']:6.1f}  "
            f"{100*r['up_share']:4.1f}%  {100*r['dn_share']:4.1f}%  {r['balance']:4.2f}  "
            f"{100*r['atr_pct']:5.2f}%  {r['efficiency']:5.3f}  {100*r['trend_ret']:6.1f}%  "
            f"{r['turnover24h']:12,.0f}"
        )

    print()
    print("Wide ersh pairs:")
    for r in top:
        print(f"{r['pair']}  ({r['category']}, ATR {100*r['atr_pct']:.2f}%)")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"generated": time.time(), "results": top}, f, indent=2)
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
