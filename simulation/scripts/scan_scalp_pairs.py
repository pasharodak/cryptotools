#!/usr/bin/env python3
"""Scan pairs for continuous small oscillations suitable for scalping.

Ranks by choppiness quality on 1m candles:
  - low Kaufman efficiency ratio (lots of path vs net move)
  - frequent EMA midline crosses
  - negative short-lag return autocorrelation (mean-reversion)
  - ATR% in a fee-aware band (enough move, not spike-regime)
  - persistence: share of rolling windows that stay choppy

Usage:
  python simulation/scripts/scan_scalp_pairs.py --pairs prod200
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

try:
    import talib.abstract as ta
except ImportError:  # pragma: no cover
    ta = None  # type: ignore


def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if ta is not None:
        return ta.ADX(df, timeperiod=period)
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr = _wilder_atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def _precompute(df: pd.DataFrame, ema_period: int = 20) -> dict[str, pd.Series]:
    close = df["close"].astype(float)
    ret = close.pct_change()
    ema = close.ewm(span=ema_period, adjust=False).mean()
    above = close > ema
    cross_flag = (above != above.shift(1)).fillna(False).astype(float)
    atr = _wilder_atr(df, 14)
    adx = _adx(df, 14)
    return {
        "close": close,
        "ret": ret,
        "ema": ema,
        "cross_flag": cross_flag,
        "atr": atr,
        "adx": adx,
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
    }


def window_metrics_pre(
    pre: dict[str, pd.Series],
    start: int,
    end: int,
) -> dict[str, float] | None:
    """Metrics for [start:end) using precomputed series."""
    n = end - start
    if n < 40:
        return None
    close = pre["close"].iloc[start:end]
    ret = pre["ret"].iloc[start:end].dropna()
    if len(ret) < 20:
        return None

    net = float(abs(close.iloc[-1] - close.iloc[0]))
    path = float(close.diff().abs().sum())
    er = net / path if path > 1e-12 else 1.0

    crosses = float(pre["cross_flag"].iloc[start:end].sum())
    hours = max(n / 60.0, 1e-6)
    crosses_per_hour = crosses / hours

    if float(ret.std()) < 1e-12:
        ac1 = 0.0
    else:
        ac_raw = ret.autocorr(lag=1)
        ac1 = float(ac_raw) if ac_raw is not None and np.isfinite(ac_raw) else 0.0

    atr_last = float(pre["atr"].iloc[end - 1])
    if not np.isfinite(atr_last):
        atr_last = 0.0
    px = float(close.iloc[-1])
    atr_pct = atr_last / px if px > 0 else 0.0

    adx_last = float(pre["adx"].iloc[end - 1])
    if not np.isfinite(adx_last):
        adx_last = 50.0

    ret_std = float(ret.std())
    spike_share = float((ret.abs() > 3 * ret_std).mean()) if ret_std > 0 else 0.0
    hi = float(pre["high"].iloc[start:end].max())
    lo = float(pre["low"].iloc[start:end].min())
    range_pct = (hi - lo) / px if px > 0 else 0.0

    return {
        "efficiency": er,
        "crosses": crosses,
        "crosses_per_hour": crosses_per_hour,
        "autocorr_1": ac1,
        "atr_pct": atr_pct,
        "adx": adx_last,
        "ret_std": ret_std,
        "spike_share": spike_share,
        "range_pct": range_pct,
        "n_bars": float(n),
    }


def window_metrics(df: pd.DataFrame, ema_period: int = 20) -> dict[str, float] | None:
    """Metrics for one OHLCV window (caller passes already-sliced frame)."""
    pre = _precompute(df, ema_period=ema_period)
    return window_metrics_pre(pre, 0, len(df))


def window_passes(m: dict[str, float], cfg: dict[str, Any]) -> bool:
    if m["efficiency"] > float(cfg["max_efficiency"]):
        return False
    if m["crosses_per_hour"] < float(cfg["min_crosses_per_hour"]):
        return False
    if m["autocorr_1"] > float(cfg["max_autocorr"]):
        return False
    if m["atr_pct"] < float(cfg["atr_pct_min"]) or m["atr_pct"] > float(cfg["atr_pct_max"]):
        return False
    if m["adx"] > float(cfg["max_adx"]):
        return False
    if m["spike_share"] > float(cfg["max_spike_share"]):
        return False
    return True


def score_pair(agg: dict[str, float], persistence: float, cfg: dict[str, Any]) -> float:
    """Higher = better continuous small oscillations."""
    er = float(agg["efficiency"])
    cph = float(agg["crosses_per_hour"])
    ac = float(agg["autocorr_1"])
    atr = float(agg["atr_pct"])
    adx = float(agg["adx"])

    er_score = 1.0 - min(er / max(float(cfg["max_efficiency"]), 1e-6), 1.0)
    cross_score = min(cph / max(float(cfg["target_crosses_per_hour"]), 1e-6), 1.5) / 1.5
    ac_clipped = max(min(ac, 0.2), -0.4)
    ac_score = (0.2 - ac_clipped) / 0.6

    lo, hi = float(cfg["atr_pct_min"]), float(cfg["atr_pct_max"])
    mid = float(cfg.get("atr_pct_ideal", (lo + hi) / 2))
    atr_score = 1.0 - min(abs(atr - mid) / max(mid - lo, 1e-9), 1.0)

    adx_score = max(0.0, 1.0 - adx / max(float(cfg["max_adx"]), 1.0))
    pers_score = float(persistence)

    return float(
        0.28 * er_score
        + 0.22 * cross_score
        + 0.20 * ac_score
        + 0.12 * atr_score
        + 0.08 * adx_score
        + 0.10 * pers_score
    )


def analyze_pair(df: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any] | None:
    window = int(cfg["window_bars"])
    step = int(cfg["step_bars"])
    if len(df) < window + 30:
        return None

    lookback = int(cfg.get("lookback_bars") or 0)
    if lookback > 0 and len(df) > lookback:
        df = df.iloc[-lookback:]

    pre = _precompute(df, ema_period=int(cfg["ema_period"]))
    metrics: list[dict[str, float]] = []
    passes = 0
    total = 0
    for start in range(0, len(df) - window + 1, step):
        m = window_metrics_pre(pre, start, start + window)
        if m is None:
            continue
        total += 1
        metrics.append(m)
        if window_passes(m, cfg):
            passes += 1

    if total < int(cfg["min_windows"]):
        return None

    persistence = passes / total
    keys = [
        "efficiency",
        "crosses_per_hour",
        "autocorr_1",
        "atr_pct",
        "adx",
        "ret_std",
        "spike_share",
        "range_pct",
    ]
    med = {k: float(np.nanmedian([m[k] for m in metrics])) for k in keys}
    for k, v in list(med.items()):
        if not np.isfinite(v):
            med[k] = 0.0

    last = metrics[-1]
    last_ok = window_passes(last, cfg)

    sc = score_pair(med, persistence, cfg)
    if persistence < float(cfg["min_persistence"]):
        sc *= 0.55 + 0.45 * (persistence / max(float(cfg["min_persistence"]), 1e-6))

    atr_pct_display = med["atr_pct"] * 100
    fee_rt = float(cfg.get("fee_rt_pct", 0.11))
    atr_vs_fees = atr_pct_display / fee_rt if fee_rt > 0 else 0.0

    return {
        "n_windows": total,
        "n_pass": passes,
        "persistence": round(persistence, 4),
        "score": round(sc, 6),
        "last_ok": last_ok,
        "med_efficiency": round(med["efficiency"], 4),
        "med_crosses_per_hour": round(med["crosses_per_hour"], 3),
        "med_autocorr_1": round(med["autocorr_1"], 4),
        "med_atr_pct": round(atr_pct_display, 4),
        "med_adx": round(med["adx"], 2),
        "med_spike_share": round(med["spike_share"], 4),
        "med_range_pct": round(med["range_pct"] * 100, 3),
        "atr_vs_fees": round(atr_vs_fees, 3),
        "fee_risk": atr_vs_fees < 1.0,
        "last_efficiency": round(last["efficiency"], 4),
        "last_atr_pct": round(last["atr_pct"] * 100, 4),
        "last_adx": round(last["adx"], 2),
        "last_autocorr_1": round(last["autocorr_1"], 4),
    }


def default_cfg() -> dict[str, Any]:
    return {
        "timeframe": "1m",
        "window_bars": 120,
        "step_bars": 60,
        "lookback_bars": 14 * 24 * 60,
        "ema_period": 20,
        "min_windows": 8,
        # Kaufman ER: lower = more back-and-forth. 0.40 still allows mild drift.
        "max_efficiency": 0.40,
        "min_crosses_per_hour": 2.0,
        "target_crosses_per_hour": 6.0,
        # Mild positive lag-1 autocorr is common on 1m; only reject strong momentum.
        "max_autocorr": 0.05,
        # 1m ATR% band: majors often ~0.04–0.10%; alts higher.
        # Floor is fee-aware soft (taker RT ~0.11% — need several ATR or maker).
        "atr_pct_min": 0.00035,
        "atr_pct_max": 0.012,
        "atr_pct_ideal": 0.0015,
        "max_adx": 32.0,
        "max_spike_share": 0.10,
        "min_persistence": 0.40,
        # Bybit taker round-trip ~0.11% (open+close). Used for atr_vs_fees only.
        "fee_rt_pct": 0.11,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-persistence", type=float, default=None)
    ap.add_argument("--max-efficiency", type=float, default=None)
    ap.add_argument("--atr-min", type=float, default=None, help="ATR fraction, e.g. 0.0008")
    ap.add_argument("--atr-max", type=float, default=None)
    ap.add_argument("--out", default=str(ROOT / "simulation/data/scalp_pair_scan.json"))
    args = ap.parse_args()

    cfg = default_cfg()
    cfg["timeframe"] = args.timeframe
    if args.min_persistence is not None:
        cfg["min_persistence"] = args.min_persistence
    if args.max_efficiency is not None:
        cfg["max_efficiency"] = args.max_efficiency
    if args.atr_min is not None:
        cfg["atr_pct_min"] = args.atr_min
    if args.atr_max is not None:
        cfg["atr_pct_max"] = args.atr_max

    pairs = pairs_from_source(ROOT, args.pairs)
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for i, pair in enumerate(pairs, 1):
        if args.limit and i > args.limit:
            break
        base = pair.split("/")[0]
        try:
            raw = ds.load(pair, cfg["timeframe"])
        except FileNotFoundError:
            skipped.append({"pair": pair, "reason": "no_data"})
            print(f"[{i}/{len(pairs)}] SKIP no data {base}", flush=True)
            continue
        if raw is None or raw.empty:
            skipped.append({"pair": pair, "reason": "empty"})
            continue

        df = raw.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")

        res = analyze_pair(df, cfg)
        if res is None:
            skipped.append({"pair": pair, "reason": "thin"})
            print(f"[{i}/{len(pairs)}] SKIP thin {base} n={len(df)}", flush=True)
            continue

        row = {
            "pair": pair,
            "base": base,
            "n_bars": int(len(df)),
            "start": str(df.index[0]),
            "end": str(df.index[-1]),
            **res,
            "pass_persistence": res["persistence"] >= float(cfg["min_persistence"]),
        }
        rows.append(row)
        flag = "OK" if row["pass_persistence"] else "weak"
        print(
            f"[{i}/{len(pairs)}] {base:12s} score={res['score']:.3f} "
            f"pers={res['persistence']*100:5.1f}% er={res['med_efficiency']:.3f} "
            f"x/h={res['med_crosses_per_hour']:.1f} ac={res['med_autocorr_1']:+.3f} "
            f"atr={res['med_atr_pct']:.3f}% adx={res['med_adx']:.1f} [{flag}]",
            flush=True,
        )

    # Stable sort: NaN scores sink to bottom
    def _sort_key(r: dict[str, Any]) -> float:
        s = r.get("score")
        try:
            v = float(s)
        except (TypeError, ValueError):
            return float("-inf")
        return v if np.isfinite(v) else float("-inf")

    rows.sort(key=_sort_key, reverse=True)
    passed = [r for r in rows if r["pass_persistence"] and np.isfinite(float(r.get("score") or 0))]
    scalp_ready = [r for r in passed if not r.get("fee_risk")]

    scores_finite = [float(r["score"]) for r in rows if np.isfinite(float(r.get("score") or np.nan))]
    summary = {
        "n_pairs_scored": len(rows),
        "n_skipped": len(skipped),
        "n_pass_persistence": len(passed),
        "n_scalp_ready": len(scalp_ready),
        "pct_pass": round(100.0 * len(passed) / len(rows), 1) if rows else 0.0,
        "pct_scalp_ready": round(100.0 * len(scalp_ready) / len(rows), 1) if rows else 0.0,
        "median_score_all": round(float(np.median(scores_finite)), 4) if scores_finite else None,
        "median_persistence_all": round(float(np.median([r["persistence"] for r in rows])), 4)
        if rows
        else None,
        "median_atr_pct_pass": round(float(np.median([r["med_atr_pct"] for r in passed])), 4)
        if passed
        else None,
        "median_crosses_per_hour_pass": round(
            float(np.median([r["med_crosses_per_hour"] for r in passed])), 3
        )
        if passed
        else None,
    }

    out = {
        "meta": {
            "pairs_source": args.pairs,
            "cfg": cfg,
            "note": (
                "Score ranks continuous small oscillations for scalping. "
                "pass_persistence = share of 2h windows that stay choppy >= min_persistence. "
                "scalp_ready = pass_persistence AND atr_vs_fees >= 1 (1m ATR% >= fee round-trip). "
                "med_atr_pct is percent (0.25 = 0.25%)."
            ),
        },
        "summary": summary,
        "top_pairs": (scalp_ready[:40] or passed[:40] or rows[:40]),
        "bottom_pairs": sorted(rows, key=lambda r: r["score"])[:20],
        "pairs": rows,
        "skipped": skipped,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = out_path.with_suffix(".csv")
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\nTOP 15 scalp-ready (pers OK + ATR >= fees):")
    show = scalp_ready[:15] if scalp_ready else (passed[:15] if passed else rows[:15])
    for r in show:
        print(
            f"  {r['base']:12s} score={r['score']:.3f} pers={r['persistence']*100:5.1f}% "
            f"er={r['med_efficiency']:.3f} x/h={r['med_crosses_per_hour']:.1f} "
            f"ac={r['med_autocorr_1']:+.3f} atr={r['med_atr_pct']:.3f}% "
            f"atr/fee={r.get('atr_vs_fees', 0):.2f}"
        )
    print(f"\nWrote {out_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
