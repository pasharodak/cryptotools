#!/usr/bin/env python3
"""Sweep hourly profit-exit thresholds (default 1..7%) on SL/TP trade cache.

One OHLC pass per trade; evaluates all thresholds. Check interval default 60m.
Also reports winners-only (sim_pnl>0, n>=20) per threshold.
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
    pnl_usdt,
    price_profit_pct,
    summarize,
)

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp_fee001"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/hourly_tp_sweep"


def bar_times_ms(index: pd.DatetimeIndex) -> np.ndarray:
    raw = index.asi8
    if len(raw) and int(raw[0]) > 10**15:
        return (raw // 10**6).astype(np.int64)
    return raw.astype(np.int64)


def first_hourly_hit(
    *,
    closes: np.ndarray,
    times: np.ndarray,
    open_ms: int,
    close_ms: int,
    open_rate: float,
    is_short: bool,
    profit_pct: float,
    check_every_ms: int,
) -> dict[str, Any] | None:
    if len(closes) == 0 or open_rate <= 0:
        return None
    next_check = open_ms + check_every_ms
    for i in range(len(closes)):
        c = float(closes[i])
        t_ms = int(times[i])
        while t_ms >= next_check:
            pp = price_profit_pct(open_rate, c, is_short)
            if pp > profit_pct:
                return {
                    "exit_ms": t_ms,
                    "exit_rate": c,
                    "profit_pct_at_exit": round(pp, 4),
                    "pnl": pnl_usdt(open_rate=open_rate, exit_rate=c, is_short=is_short),
                }
            next_check += check_every_ms
            if next_check > close_ms:
                return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--scenarios", default="new_psar,trend_macd_ema,trend_supertrend")
    ap.add_argument("--levels", default="1,2,3,4,5,6,7")
    ap.add_argument("--check-min", type=int, default=60)
    ap.add_argument("--split", default="test", choices=("test", "train", "both"))
    ap.add_argument("--min-pair-n", type=int, default=20)
    args = ap.parse_args()

    levels = [float(x.strip()) for x in args.levels.split(",") if x.strip()]
    want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    cells = [c for c in discover_cells(Path(args.cache_dir)) if c["scenario_id"] in want]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    ohlc_cache: dict[str, pd.DataFrame] = {}
    check_ms = int(args.check_min) * 60 * 1000
    splits = ["train", "test"] if args.split == "both" else [args.split]

    print(
        f"=== hourly TP sweep levels={levels} every {args.check_min}m · "
        f"cells={len(cells)} · split={args.split} ===",
        flush=True,
    )

    # (sid, pair) -> per level
    pair_agg: dict[tuple[str, str], dict[float, dict[str, float]]] = defaultdict(
        lambda: {lv: {"orig": 0.0, "sim": 0.0, "n": 0, "hourly": 0} for lv in levels}
    )
    by_sc: dict[float, dict[str, dict[str, float]]] = {
        lv: defaultdict(lambda: {"orig": 0.0, "sim": 0.0, "hourly": 0, "n": 0}) for lv in levels
    }
    # per cell key results
    cell_rows: dict[float, list[dict[str, Any]]] = {lv: [] for lv in levels}

    t0 = time.time()
    for i, cell in enumerate(cells, 1):
        sid = cell["scenario_id"]
        key = cell["key"]
        orig_list: list[float] = []
        sim_lists: dict[float, list[float]] = {lv: [] for lv in levels}
        hourly_n: dict[float, int] = {lv: 0 for lv in levels}

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
                if not pair or open_ms <= 0 or open_rate <= 0:
                    continue
                if pair not in ohlc_cache:
                    try:
                        ohlc_cache[pair] = ds.load(pair, "5m")
                    except Exception:
                        ohlc_cache[pair] = pd.DataFrame()

                df = ohlc_cache[pair]
                closes = np.asarray([], dtype=float)
                times = np.asarray([], dtype=np.int64)
                if df is not None and not df.empty:
                    start_ts = pd.Timestamp(open_ms, unit="ms", tz=UTC)
                    end_ms = close_ms if close_ms > open_ms else open_ms + 7 * 86400_000
                    end_ts = pd.Timestamp(end_ms, unit="ms", tz=UTC)
                    after = df[(df.index > start_ts) & (df.index <= end_ts)]
                    if len(after):
                        closes = after["close"].to_numpy(dtype=float)
                        times = bar_times_ms(after.index)

                orig_list.append(orig)
                pk = (sid, pair)
                for lv in levels:
                    hit = first_hourly_hit(
                        closes=closes,
                        times=times,
                        open_ms=open_ms,
                        close_ms=close_ms if close_ms > open_ms else open_ms + 7 * 86400_000,
                        open_rate=open_rate,
                        is_short=is_short,
                        profit_pct=lv,
                        check_every_ms=check_ms,
                    )
                    sim = float(hit["pnl"]) if hit else orig
                    sim_lists[lv].append(sim)
                    if hit:
                        hourly_n[lv] += 1
                    pa = pair_agg[pk][lv]
                    pa["orig"] += orig
                    pa["sim"] += sim
                    pa["n"] += 1
                    if hit:
                        pa["hourly"] += 1
                    sc = by_sc[lv][sid]
                    sc["orig"] += orig
                    sc["sim"] += sim
                    sc["n"] += 1
                    if hit:
                        sc["hourly"] += 1

        o_sum = summarize(orig_list)
        for lv in levels:
            s_sum = summarize(sim_lists[lv])
            row = {
                "key": key,
                "scenario_id": sid,
                "tp_pct": cell["tp_pct"],
                "sl_pct": cell["sl_pct"],
                "profit_pct": lv,
                "orig": o_sum,
                "sim": s_sum,
                "delta_pnl": round(s_sum["pnl"] - o_sum["pnl"], 4),
                "n_hourly": hourly_n[lv],
                "hourly_rate": round(hourly_n[lv] / max(1, o_sum["n"]), 4),
            }
            cell_rows[lv].append(row)

        deltas = " ".join(
            f"tp{lv:g}={summarize(sim_lists[lv])['pnl'] - o_sum['pnl']:+.0f}" for lv in levels
        )
        print(f"[{i}/{len(cells)}] {key}: orig={o_sum['pnl']:+.1f} | {deltas}", flush=True)

    # build sweep summary
    sweep_rows = []
    for lv in levels:
        rows = cell_rows[lv]
        sum_orig = sum(float(r["orig"]["pnl"]) for r in rows)
        sum_sim = sum(float(r["sim"]["pnl"]) for r in rows)
        # winners-only across pairs
        win_sim = 0.0
        win_orig = 0.0
        n_win = 0
        n_lose = 0
        for (sid, pair), by_lv in pair_agg.items():
            pa = by_lv[lv]
            if pa["n"] < args.min_pair_n:
                continue
            if pa["sim"] > 0:
                win_sim += pa["sim"]
                win_orig += pa["orig"]
                n_win += 1
            else:
                n_lose += 1
        best_cell = max(rows, key=lambda r: float(r["sim"]["pnl"]))
        sweep_rows.append(
            {
                "profit_pct": lv,
                "sum_orig": round(sum_orig, 2),
                "sum_sim": round(sum_sim, 2),
                "sum_delta": round(sum_sim - sum_orig, 2),
                "total_hourly": sum(int(r["n_hourly"]) for r in rows),
                "cells_improved": sum(1 for r in rows if r["delta_pnl"] > 0),
                "n_cells": len(rows),
                "winners_n": n_win,
                "losers_n": n_lose,
                "winners_only_sim": round(win_sim, 2),
                "winners_only_orig": round(win_orig, 2),
                "best_cell": {
                    "key": best_cell["key"],
                    "sim": best_cell["sim"]["pnl"],
                    "delta": best_cell["delta_pnl"],
                    "hourly": best_cell["n_hourly"],
                },
                "by_scenario": {
                    sid: {
                        "orig": round(v["orig"], 2),
                        "sim": round(v["sim"], 2),
                        "delta": round(v["sim"] - v["orig"], 2),
                        "hourly": int(v["hourly"]),
                        "n": int(v["n"]),
                    }
                    for sid, v in sorted(by_sc[lv].items())
                },
            }
        )

    # top pairs per level (by sim)
    top_pairs = {}
    for lv in levels:
        plist = []
        for (sid, pair), by_lv in pair_agg.items():
            pa = by_lv[lv]
            if pa["n"] < args.min_pair_n:
                continue
            plist.append(
                {
                    "scenario_id": sid,
                    "pair": pair,
                    "n": int(pa["n"]),
                    "orig": round(pa["orig"], 2),
                    "sim": round(pa["sim"], 2),
                    "delta": round(pa["sim"] - pa["orig"], 2),
                    "hourly": int(pa["hourly"]),
                }
            )
        plist.sort(key=lambda x: x["sim"], reverse=True)
        top_pairs[str(lv)] = plist[:25]

    summary = {
        "updated": datetime.now(tz=UTC).isoformat(),
        "check_min": args.check_min,
        "levels": levels,
        "split": args.split,
        "fee": FEE,
        "leverage": LEV,
        "stake": STAKE,
        "min_pair_n": args.min_pair_n,
        "elapsed_sec": round(time.time() - t0, 1),
        "sweep": sweep_rows,
        "top_pairs_by_level": top_pairs,
        "best_level_by_sum_sim": max(sweep_rows, key=lambda r: r["sum_sim"]),
        "best_level_by_winners_only": max(sweep_rows, key=lambda r: r["winners_only_sim"]),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        f"# Hourly profit-exit sweep (every {args.check_min}m)",
        "",
        f"Split `{args.split}` · stake={STAKE} lev={LEV} fee={FEE}x2",
        "",
        "| TP% | all sim | Δ vs orig | hourly exits | winners n | winners-only sim | best cell |",
        "|-----|---------|-----------|--------------|-----------|------------------|-----------|",
    ]
    for r in sweep_rows:
        md.append(
            f"| {r['profit_pct']:g} | {r['sum_sim']:+.1f} | {r['sum_delta']:+.1f} | "
            f"{r['total_hourly']} | {r['winners_n']} | {r['winners_only_sim']:+.1f} | "
            f"`{r['best_cell']['key']}` {r['best_cell']['sim']:+.1f} |"
        )
    best_all = summary["best_level_by_sum_sim"]
    best_win = summary["best_level_by_winners_only"]
    md.extend(
        [
            "",
            f"- Best **all pairs** sim: **{best_all['profit_pct']:g}%** → {best_all['sum_sim']:+.1f}",
            f"- Best **winners-only**: **{best_win['profit_pct']:g}%** → {best_win['winners_only_sim']:+.1f} "
            f"({best_win['winners_n']} pairs)",
            "",
        ]
    )
    (out_dir / "comparison.md").write_text("\n".join(md), encoding="utf-8")

    print("\n=== SWEEP ===", flush=True)
    for r in sweep_rows:
        print(
            f"  TP{r['profit_pct']:g}%: all_sim={r['sum_sim']:+.1f} d={r['sum_delta']:+.1f} "
            f"hourly={r['total_hourly']} winners={r['winners_n']} "
            f"win_only={r['winners_only_sim']:+.1f}",
            flush=True,
        )
    print(
        f"best all={best_all['profit_pct']:g}% ({best_all['sum_sim']:+.1f}) · "
        f"best winners-only={best_win['profit_pct']:g}% ({best_win['winners_only_sim']:+.1f})",
        flush=True,
    )
    print(f"Saved {out_dir / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
