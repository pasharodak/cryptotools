#!/usr/bin/env python3
"""Find Bybit spot pairs similar to a reference (chart shape + turnover).

Usage:
  python simulation/scripts/scan_similar_spot.py --ref CAPUSDT
"""
from __future__ import annotations

import argparse
import json
import math
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


def fetch_tickers() -> dict[str, dict[str, Any]]:
    data = http_get(BYBIT_TICKERS)
    out: dict[str, dict[str, Any]] = {}
    for t in data.get("result", {}).get("list", []) or []:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        try:
            turn = float(t.get("turnover24h") or 0)
            last = float(t.get("lastPrice") or 0)
            chg = float(t.get("price24hPcnt") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        out[sym] = {"symbol": sym, "turnover24h": turn, "lastPrice": last, "chg24h": chg}
    return out


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    q = urllib.parse.urlencode(
        {"category": "spot", "symbol": symbol, "interval": interval, "limit": str(limit)}
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


def profile(df: pd.DataFrame) -> dict[str, float] | None:
    if len(df) < 100:
        return None
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    turn = df["turnover"].astype(float)

    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bot = pd.concat([o, c], axis=1).min(axis=1)
    upper_wick = (h - body_top).clip(lower=0)
    lower_wick = (body_bot - l).clip(lower=0)
    full_range = (h - l).replace(0, np.nan)
    atr = _wilder_atr(df, 14)
    atr_prev = atr.shift(1).replace(0, np.nan)

    wick_vs_atr = upper_wick / atr_prev
    wick_vs_range = upper_wick / full_range
    spike_wick = (wick_vs_atr >= 1.2) | (wick_vs_range >= 0.55)
    prior_max = h.shift(1).rolling(12, min_periods=6).max()
    pierce = (h > prior_max * 1.002) & (wick_vs_atr >= 0.6)
    valid = atr_prev.notna()
    spikes = (spike_wick | pierce).fillna(False) & valid
    n_valid = int(valid.sum())
    if n_valid < 80:
        return None

    spike_share = float(spikes.sum() / n_valid)
    spike_sizes = wick_vs_atr[spikes].dropna()
    avg_spike_atr = float(spike_sizes.mean()) if len(spike_sizes) else 0.0

    # lower wick share (for shape — CAP-like often has both)
    low_wick_vs = lower_wick / atr_prev
    lower_spike = ((low_wick_vs >= 1.2) | ((lower_wick / full_range) >= 0.55)).fillna(False) & valid
    lower_spike_share = float(lower_spike.sum() / n_valid)

    ret = c.pct_change().dropna()
    ret_std = float(ret.std()) if len(ret) else 0.0
    ret_skew = float(ret.skew()) if len(ret) > 20 else 0.0
    abs_ret = ret.abs()
    jump_share = float((abs_ret > 3 * ret_std).mean()) if ret_std > 0 else 0.0

    px = float(c.iloc[-1])
    atr_last = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    atr_pct = atr_last / px if px > 0 else 0.0

    hi = float(h.max())
    lo = float(l.min())
    range_pct = (hi - lo) / px if px > 0 else 0.0
    trend_ret = float(c.iloc[-1] / c.iloc[0] - 1.0) if float(c.iloc[0]) > 0 else 0.0

    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema50_slope = float(ema50.iloc[-1] / ema50.iloc[-73] - 1.0) if len(ema50) > 73 else 0.0
    above_ema = float((c > ema50).tail(96).mean())
    bull_stack = 1.0 if float(c.iloc[-1]) > float(ema20.iloc[-1]) > float(ema50.iloc[-1]) else 0.0

    # choppiness / path efficiency
    net = abs(float(c.iloc[-1] - c.iloc[0]))
    path = float(c.diff().abs().sum())
    efficiency = net / path if path > 1e-12 else 1.0

    # volume rhythm
    turn_med = float(turn.median()) if len(turn) else 0.0
    turn_cv = float(turn.std() / turn_med) if turn_med > 0 else 0.0
    vol_spike_share = float((turn > 3 * turn_med).mean()) if turn_med > 0 else 0.0

    # autocorrelation (momentum vs mean-revert)
    ac1 = float(ret.autocorr(lag=1) or 0.0) if len(ret) > 30 else 0.0
    if not math.isfinite(ac1):
        ac1 = 0.0

    hours = n_valid * 5 / 60.0
    return {
        "spike_share": spike_share,
        "spikes_per_day": float(spikes.sum()) / max(hours / 24.0, 1e-6),
        "avg_spike_atr": avg_spike_atr,
        "lower_spike_share": lower_spike_share,
        "atr_pct": atr_pct,
        "range_pct": range_pct,
        "trend_ret": trend_ret,
        "ema50_slope": ema50_slope,
        "above_ema50": above_ema,
        "bull_stack": bull_stack,
        "ret_std": ret_std,
        "ret_skew": ret_skew,
        "jump_share": jump_share,
        "efficiency": efficiency,
        "turn_cv": turn_cv,
        "vol_spike_share": vol_spike_share,
        "ac1": ac1,
        "bars": float(n_valid),
    }


# Feature weights for "same chart feel" vs volume handled separately
CHART_FEATURES = [
    ("spike_share", 2.5),
    ("avg_spike_atr", 1.5),
    ("lower_spike_share", 1.2),
    ("atr_pct", 2.0),
    ("range_pct", 1.5),
    ("trend_ret", 1.8),
    ("ema50_slope", 1.2),
    ("above_ema50", 1.0),
    ("ret_std", 2.0),
    ("ret_skew", 1.0),
    ("jump_share", 1.5),
    ("efficiency", 1.5),
    ("turn_cv", 1.0),
    ("vol_spike_share", 1.2),
    ("ac1", 1.0),
]


def similarity(
    ref: dict[str, float],
    other: dict[str, float],
    *,
    ref_turn: float,
    other_turn: float,
    volume_band: float,
) -> dict[str, float] | None:
    # volume gate: within factor band of reference turnover
    if ref_turn <= 0 or other_turn <= 0:
        return None
    turn_ratio = other_turn / ref_turn
    if turn_ratio < 1 / volume_band or turn_ratio > volume_band:
        return None

    # log-volume distance (0 = identical)
    vol_dist = abs(math.log(turn_ratio))

    chart_dist = 0.0
    wsum = 0.0
    for key, w in CHART_FEATURES:
        a = float(ref.get(key, 0.0))
        b = float(other.get(key, 0.0))
        scale = max(abs(a), abs(b), 1e-6)
        # relative absolute error, clipped
        d = min(abs(a - b) / scale, 3.0)
        chart_dist += w * d
        wsum += w
    chart_dist /= max(wsum, 1e-9)

    # combined: chart dominates, volume soft penalty
    dist = chart_dist + 0.35 * vol_dist
    score = 100.0 * math.exp(-dist)  # 100 = identical
    return {
        "score": score,
        "chart_dist": chart_dist,
        "vol_dist": vol_dist,
        "turn_ratio": turn_ratio,
    }


def load_profile(symbol: str, interval: str, limit: int) -> dict[str, float] | None:
    df = fetch_klines(symbol, interval, limit)
    if df.empty:
        return None
    return profile(df)


def main() -> int:
    ap = argparse.ArgumentParser(description="Find spot pairs similar to a reference")
    ap.add_argument("--ref", default="CAPUSDT")
    ap.add_argument("--interval", default="5")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument(
        "--volume-band",
        type=float,
        default=4.0,
        help="Keep pairs with turnover within 1/N .. N of reference",
    )
    ap.add_argument("--min-turnover", type=float, default=200_000.0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    tickers = fetch_tickers()
    ref_sym = args.ref.upper().replace("/", "").replace(":USDT", "")
    if not ref_sym.endswith("USDT"):
        ref_sym += "USDT"
    if ref_sym not in tickers:
        print(f"Reference {ref_sym} not found on Bybit spot", file=sys.stderr)
        return 1

    ref_t = tickers[ref_sym]
    print(
        f"Reference {ref_sym}: last={ref_t['lastPrice']}  "
        f"chg24h={100*ref_t['chg24h']:.2f}%  turnover24h={ref_t['turnover24h']:,.0f}",
        flush=True,
    )
    ref_prof = load_profile(ref_sym, args.interval, args.limit)
    if not ref_prof:
        print("Failed to profile reference klines", file=sys.stderr)
        return 1

    print(
        f"  spike%={100*ref_prof['spike_share']:.1f}  spikes/d={ref_prof['spikes_per_day']:.1f}  "
        f"ATR%={100*ref_prof['atr_pct']:.3f}  range%={100*ref_prof['range_pct']:.1f}  "
        f"trend%={100*ref_prof['trend_ret']:.1f}  eff={ref_prof['efficiency']:.3f}",
        flush=True,
    )

    candidates = [
        t
        for sym, t in tickers.items()
        if sym != ref_sym
        and t["turnover24h"] >= args.min_turnover
        and (1 / args.volume_band) * ref_t["turnover24h"]
        <= t["turnover24h"]
        <= args.volume_band * ref_t["turnover24h"]
    ]
    print(
        f"Scanning {len(candidates)} pairs in volume band "
        f"[{ref_t['turnover24h']/args.volume_band:,.0f} .. {ref_t['turnover24h']*args.volume_band:,.0f}]...",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    done = 0

    def work(row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            prof = load_profile(row["symbol"], args.interval, args.limit)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            return None
        if not prof:
            return None
        sim = similarity(
            ref_prof,
            prof,
            ref_turn=ref_t["turnover24h"],
            other_turn=row["turnover24h"],
            volume_band=args.volume_band,
        )
        if not sim:
            return None
        base = row["symbol"][: -len("USDT")]
        return {
            "symbol": row["symbol"],
            "pair": f"{base}/USDT",
            "turnover24h": row["turnover24h"],
            "chg24h": row["chg24h"],
            **prof,
            **sim,
        }

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(work, row): row["symbol"] for row in candidates}
        for fut in as_completed(futs):
            done += 1
            if done % 20 == 0 or done == len(futs):
                print(f"  progress {done}/{len(futs)}", flush=True)
            try:
                r = fut.result()
            except Exception:
                continue
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[: max(1, args.top)]

    print()
    print(
        f"{'#':>3}  {'pair':<14}  {'score':>6}  {'turn24h':>12}  {'xvol':>5}  "
        f"{'spike%':>6}  {'ATR%':>6}  {'trend%':>7}  {'eff':>5}  {'chg24h':>7}"
    )
    print("-" * 100)
    ref_pair = ref_sym[:-4] + "/USDT" if ref_sym.endswith("USDT") else ref_sym
    print(
        f"{'ref':>3}  {ref_pair:<14}  {'REF':>6}  "
        f"{ref_t['turnover24h']:12,.0f}  {'1.00':>5}  "
        f"{100*ref_prof['spike_share']:5.1f}%  {100*ref_prof['atr_pct']:5.2f}%  "
        f"{100*ref_prof['trend_ret']:6.1f}%  {ref_prof['efficiency']:5.3f}  "
        f"{100*ref_t['chg24h']:6.1f}%"
    )
    for i, r in enumerate(top, 1):
        print(
            f"{i:>3}  {r['pair']:<14}  {r['score']:6.1f}  {r['turnover24h']:12,.0f}  "
            f"{r['turn_ratio']:5.2f}  {100*r['spike_share']:5.1f}%  {100*r['atr_pct']:5.2f}%  "
            f"{100*r['trend_ret']:6.1f}%  {r['efficiency']:5.3f}  {100*r['chg24h']:6.1f}%"
        )

    print()
    print(f"Closest to {ref_sym} (spot):")
    for r in top:
        print(r["pair"])

    if args.json_out:
        payload = {
            "ref": {"symbol": ref_sym, "turnover24h": ref_t["turnover24h"], "profile": ref_prof},
            "results": top,
            "generated": time.time(),
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
