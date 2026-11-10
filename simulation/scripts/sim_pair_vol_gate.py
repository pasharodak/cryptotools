#!/usr/bin/env python3
"""Causal pair volatility gate on SL/TP trade cache (prod-like at-entry filter).

At each trade open_ms, compute NATR%/vol%/spike on past 5m bars only.
If gate fails — trade skipped. Optional hourly profit lock on kept trades.

See: simulation/results/ml_param_experiments/pair_vol_gate_spec.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.scripts.run_ml_sltp_grid import discover_cells  # noqa: E402
from simulation.scripts.sim_hourly_profit_exit import (  # noqa: E402
    FEE,
    LEV,
    STAKE,
    summarize,
    try_hourly_exit,
)

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp_fee001"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/pair_vol_gate"


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def precompute_pair(df: pd.DataFrame) -> dict[str, Any] | None:
    if df is None or df.empty or len(df) < 100:
        return None
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    atr = wilder_atr(high, low, close, 14)
    natr = atr / close.replace(0, np.nan) * 100.0
    ret = close.pct_change() * 100.0
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    spike = (tr > 3.0 * atr).astype(float)
    return {
        "index": df.index,
        "natr": natr.to_numpy(dtype=float),
        "ret": ret.to_numpy(dtype=float),
        "spike": spike.to_numpy(dtype=float),
        "times": (
            (df.index.asi8 // 10**6).astype(np.int64)
            if len(df.index) and int(df.index.asi8[0]) > 10**15
            else df.index.asi8.astype(np.int64)
        ),
    }


def gate_at(
    pre: dict[str, Any] | None,
    open_ms: int,
    *,
    lookback_bars: int,
    natr_min: float,
    vol_min: float,
    atr_vs_fee_min: float,
    fee_rt_pct: float,
    spike_max: float,
    min_bars: int,
) -> tuple[bool, dict[str, Any]]:
    if pre is None:
        return False, {"reason": "no_ohlc"}
    times = pre["times"]
    # last bar strictly before open
    idx = int(np.searchsorted(times, open_ms, side="left") - 1)
    if idx < min_bars - 1:
        return False, {"reason": "warmup", "idx": idx}
    start = max(0, idx - lookback_bars + 1)
    natr = pre["natr"][start : idx + 1]
    ret = pre["ret"][start : idx + 1]
    spike = pre["spike"][start : idx + 1]
    natr = natr[np.isfinite(natr)]
    ret = ret[np.isfinite(ret)]
    if len(natr) < min_bars or len(ret) < max(20, min_bars // 4):
        return False, {"reason": "short_window", "n": int(len(natr))}
    natr_med = float(np.median(natr))
    vol = float(np.std(ret))
    spike_share = float(np.mean(spike)) if len(spike) else 1.0
    atr_vs_fee = natr_med / fee_rt_pct if fee_rt_pct > 0 else 0.0
    meta = {
        "natr_pct": round(natr_med, 4),
        "vol_pct": round(vol, 4),
        "atr_vs_fee": round(atr_vs_fee, 3),
        "spike_share": round(spike_share, 4),
        "n_bars": int(len(natr)),
    }
    ok = (
        natr_med >= natr_min
        and vol >= vol_min
        and atr_vs_fee >= atr_vs_fee_min
        and spike_share <= spike_max
    )
    if not ok:
        meta["reason"] = "threshold"
    return ok, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--scenarios", default="new_psar,trend_macd_ema,trend_supertrend")
    ap.add_argument("--split", default="test", choices=("test", "train", "both"))
    ap.add_argument("--lookback-days", type=float, default=3.0)
    ap.add_argument("--natr-min", type=float, default=0.34)
    ap.add_argument("--vol-min", type=float, default=0.32)
    ap.add_argument("--atr-vs-fee-min", type=float, default=1.7)
    ap.add_argument("--fee-rt-pct", type=float, default=0.20)
    ap.add_argument("--spike-max", type=float, default=0.08)
    ap.add_argument("--hourly-pct", type=float, default=1.0, help="0=disable hourly lock")
    ap.add_argument("--check-min", type=int, default=60)
    ap.add_argument("--max-cells", type=int, default=0)
    args = ap.parse_args()

    want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    cells = [c for c in discover_cells(Path(args.cache_dir)) if c["scenario_id"] in want]
    if args.max_cells:
        cells = cells[: args.max_cells]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    lookback_bars = int(args.lookback_days * 288)
    min_bars = min(500, lookback_bars)
    check_ms = args.check_min * 60 * 1000
    splits = ["train", "test"] if args.split == "both" else [args.split]

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    pre_cache: dict[str, dict[str, Any] | None] = {}
    ohlc_raw: dict[str, pd.DataFrame] = {}

    print(
        f"=== pair vol gate · natr>={args.natr_min} vol>={args.vol_min} "
        f"atr/fee>={args.atr_vs_fee_min} spike<={args.spike_max} · "
        f"lookback={args.lookback_days}d · hourly={args.hourly_pct:g}%/{args.check_min}m · "
        f"cells={len(cells)} ===",
        flush=True,
    )

    cell_rows: list[dict[str, Any]] = []
    by_sc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"raw": 0.0, "gated": 0.0, "gated_hourly": 0.0, "n_raw": 0, "n_pass": 0, "n_block": 0}
    )
    t0 = time.time()

    for i, cell in enumerate(cells, 1):
        raw_pnls: list[float] = []
        gated_pnls: list[float] = []
        gated_h_pnls: list[float] = []
        n_pass = n_block = 0

        for split in splits:
            path = Path(cell["train_path"] if split == "train" else cell["test_path"])
            trades = json.loads(path.read_text(encoding="utf-8"))
            for rec in trades:
                tr = rec.get("trade") or {}
                pair = str(rec.get("pair") or "")
                open_ms = int(tr.get("open_ms") or 0)
                close_ms = int(tr.get("close_ms") or 0)
                open_rate = float(tr.get("open_rate") or 0)
                is_short = bool(tr.get("is_short"))
                orig = float(
                    rec.get("profit_abs")
                    if rec.get("profit_abs") is not None
                    else tr.get("profit_abs") or 0
                )
                if not pair or open_ms <= 0:
                    continue
                raw_pnls.append(orig)
                by_sc[cell["scenario_id"]]["raw"] += orig
                by_sc[cell["scenario_id"]]["n_raw"] += 1

                if pair not in pre_cache:
                    try:
                        df = ds.load(pair, "5m")
                    except Exception:
                        df = pd.DataFrame()
                    ohlc_raw[pair] = df
                    pre_cache[pair] = precompute_pair(df)

                ok, _meta = gate_at(
                    pre_cache[pair],
                    open_ms,
                    lookback_bars=lookback_bars,
                    natr_min=args.natr_min,
                    vol_min=args.vol_min,
                    atr_vs_fee_min=args.atr_vs_fee_min,
                    fee_rt_pct=args.fee_rt_pct,
                    spike_max=args.spike_max,
                    min_bars=min_bars,
                )
                if not ok:
                    n_block += 1
                    by_sc[cell["scenario_id"]]["n_block"] += 1
                    continue
                n_pass += 1
                by_sc[cell["scenario_id"]]["n_pass"] += 1
                gated_pnls.append(orig)
                by_sc[cell["scenario_id"]]["gated"] += orig

                sim = orig
                if args.hourly_pct > 0:
                    early = try_hourly_exit(
                        ohlc=ohlc_raw[pair],
                        open_ms=open_ms,
                        close_ms=close_ms if close_ms > open_ms else open_ms + 7 * 86400_000,
                        open_rate=open_rate,
                        is_short=is_short,
                        profit_pct=float(args.hourly_pct),
                        check_every_ms=check_ms,
                    )
                    if early is not None:
                        sim = float(early["pnl"])
                gated_h_pnls.append(sim)
                by_sc[cell["scenario_id"]]["gated_hourly"] += sim

        r_sum = summarize(raw_pnls)
        g_sum = summarize(gated_pnls)
        h_sum = summarize(gated_h_pnls)
        row = {
            "key": cell["key"],
            "scenario_id": cell["scenario_id"],
            "tp_pct": cell["tp_pct"],
            "sl_pct": cell["sl_pct"],
            "raw": r_sum,
            "vol_gated": g_sum,
            "vol_gated_hourly": h_sum,
            "n_pass": n_pass,
            "n_block": n_block,
            "pass_rate": round(n_pass / max(1, n_pass + n_block), 4),
            "delta_gate": round(g_sum["pnl"] - r_sum["pnl"], 4),
            "delta_gate_hourly": round(h_sum["pnl"] - r_sum["pnl"], 4),
        }
        cell_rows.append(row)
        print(
            f"[{i}/{len(cells)}] {cell['key']}: raw={r_sum['pnl']:+.1f} "
            f"gate={g_sum['pnl']:+.1f} gate+h={h_sum['pnl']:+.1f} "
            f"pass={n_pass}/{n_pass + n_block} ({100 * row['pass_rate']:.1f}%)",
            flush=True,
        )

    summary = {
        "updated": datetime.now(tz=UTC).isoformat(),
        "spec": "pair_vol_gate_spec.md",
        "params": {
            "natr_min": args.natr_min,
            "vol_min": args.vol_min,
            "atr_vs_fee_min": args.atr_vs_fee_min,
            "fee_rt_pct": args.fee_rt_pct,
            "spike_max": args.spike_max,
            "lookback_days": args.lookback_days,
            "hourly_pct": args.hourly_pct,
            "check_min": args.check_min,
            "split": args.split,
            "fee": FEE,
            "leverage": LEV,
            "stake": STAKE,
        },
        "sum_raw": round(sum(float(r["raw"]["pnl"]) for r in cell_rows), 2),
        "sum_vol_gated": round(sum(float(r["vol_gated"]["pnl"]) for r in cell_rows), 2),
        "sum_vol_gated_hourly": round(sum(float(r["vol_gated_hourly"]["pnl"]) for r in cell_rows), 2),
        "n_pass": sum(int(r["n_pass"]) for r in cell_rows),
        "n_block": sum(int(r["n_block"]) for r in cell_rows),
        "by_scenario": {
            sid: {
                "raw": round(v["raw"], 2),
                "gated": round(v["gated"], 2),
                "gated_hourly": round(v["gated_hourly"], 2),
                "n_raw": int(v["n_raw"]),
                "n_pass": int(v["n_pass"]),
                "n_block": int(v["n_block"]),
                "pass_rate": round(v["n_pass"] / max(1, v["n_pass"] + v["n_block"]), 4),
            }
            for sid, v in sorted(by_sc.items())
        },
        "best_cells_gated_hourly": sorted(
            cell_rows, key=lambda r: float(r["vol_gated_hourly"]["pnl"]), reverse=True
        )[:20],
        "cells": cell_rows,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        "# Pair volatility gate (causal, at-entry)",
        "",
        f"Params: NATR≥{args.natr_min} vol≥{args.vol_min} atr/fee≥{args.atr_vs_fee_min} "
        f"spike≤{args.spike_max} lookback={args.lookback_days}d · hourly>{args.hourly_pct:g}%/{args.check_min}m",
        "",
        f"- Raw (all trades): **{summary['sum_raw']:+.1f}**",
        f"- Vol-gated only: **{summary['sum_vol_gated']:+.1f}**",
        f"- Vol-gated + hourly: **{summary['sum_vol_gated_hourly']:+.1f}**",
        f"- Pass/block: {summary['n_pass']}/{summary['n_block']} "
        f"({100 * summary['n_pass'] / max(1, summary['n_pass'] + summary['n_block']):.1f}% pass)",
        "",
        "## By scenario",
        "",
    ]
    for sid, v in summary["by_scenario"].items():
        md.append(
            f"- `{sid}`: raw {v['raw']:+.1f} → gate {v['gated']:+.1f} → "
            f"gate+h {v['gated_hourly']:+.1f} · pass {v['n_pass']}/{v['n_raw']} ({100*v['pass_rate']:.1f}%)"
        )
    md.extend(["", "## Best cells (gate+hourly)", ""])
    for r in summary["best_cells_gated_hourly"][:10]:
        md.append(
            f"- `{r['key']}`: {r['vol_gated_hourly']['pnl']:+.1f} "
            f"(raw {r['raw']['pnl']:+.1f}, pass {r['pass_rate']:.0%})"
        )
    (out_dir / "comparison.md").write_text("\n".join(md), encoding="utf-8")

    print("\n=== TOTAL ===", flush=True)
    print(
        f"raw={summary['sum_raw']:+.1f} gated={summary['sum_vol_gated']:+.1f} "
        f"gated+hourly={summary['sum_vol_gated_hourly']:+.1f} "
        f"pass={summary['n_pass']} block={summary['n_block']}",
        flush=True,
    )
    for sid, v in summary["by_scenario"].items():
        print(
            f"  {sid}: raw {v['raw']:+.1f} -> gate {v['gated']:+.1f} -> "
            f"h {v['gated_hourly']:+.1f} pass={v['pass_rate']:.1%}",
            flush=True,
        )
    print(f"Saved {out_dir / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
