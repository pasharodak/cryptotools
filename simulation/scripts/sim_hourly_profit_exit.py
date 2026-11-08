#!/usr/bin/env python3
"""Add-only exit rule on cached trades:

  Every N minutes after entry, if unrealized price profit > X%, close at that bar close.
  Otherwise keep the original exit / profit_abs.

Does NOT re-simulate SL/TP (avoids mismatch with ctengine exit_signal etc.).

Default: every 60m, profit > 4% (price %), fee already inside original PnL;
early exit PnL recomputed with fee 0.1%x2, lev 3, stake 15 (prod-like).
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

DEFAULT_CACHE = ROOT / "simulation/results/ml_param_experiments/trade_cache_sltp_fee001"
DEFAULT_OUT = ROOT / "simulation/results/ml_param_experiments/hourly_tp4_exit"
FEE = 0.001
STAKE = 15.0
LEV = 3.0


def summarize(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"n": 0, "wins": 0, "losses": 0, "winrate": 0.0, "pnl": 0.0}
    arr = np.asarray(pnls, dtype=float)
    wins = int((arr >= 0).sum())
    n = len(arr)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "winrate": round(wins / n, 4),
        "pnl": round(float(arr.sum()), 4),
    }


def pnl_usdt(*, open_rate: float, exit_rate: float, is_short: bool) -> float:
    if open_rate <= 0:
        return 0.0
    if is_short:
        price_move = (open_rate - exit_rate) / open_rate
    else:
        price_move = (exit_rate - open_rate) / open_rate
    return STAKE * LEV * (price_move - 2.0 * FEE)


def price_profit_pct(open_rate: float, px: float, is_short: bool) -> float:
    if open_rate <= 0:
        return 0.0
    if is_short:
        return (open_rate - px) / open_rate * 100.0
    return (px - open_rate) / open_rate * 100.0


def try_hourly_exit(
    *,
    ohlc: pd.DataFrame,
    open_ms: int,
    close_ms: int,
    open_rate: float,
    is_short: bool,
    profit_pct: float,
    check_every_ms: int,
) -> dict[str, Any] | None:
    """Return early exit if hourly rule hits before original close_ms; else None."""
    if ohlc is None or ohlc.empty or open_rate <= 0 or open_ms <= 0:
        return None
    start_ts = pd.Timestamp(open_ms, unit="ms", tz=UTC)
    end_ts = pd.Timestamp(close_ms, unit="ms", tz=UTC) if close_ms > open_ms else None
    after = ohlc[ohlc.index > start_ts]
    if end_ts is not None:
        after = after[after.index <= end_ts]
    if after.empty:
        return None

    next_check = open_ms + check_every_ms
    closes = after["close"].to_numpy(dtype=float)
    # DatetimeIndex.asi8 is ns on normal pandas; some feather loads store ms-scale ints.
    raw = after.index.asi8
    times = (raw // 10**6).astype(np.int64) if len(raw) and int(raw[0]) > 10**15 else raw.astype(np.int64)

    for i in range(len(after)):
        c = float(closes[i])
        t_ms = int(times[i])
        while t_ms >= next_check:
            pp = price_profit_pct(open_rate, c, is_short)
            if pp > profit_pct:
                return {
                    "exit_reason": "hourly_tp",
                    "exit_ms": t_ms,
                    "exit_rate": c,
                    "profit_pct_at_exit": round(pp, 4),
                    "pnl": pnl_usdt(open_rate=open_rate, exit_rate=c, is_short=is_short),
                }
            next_check += check_every_ms
            if next_check > close_ms:
                return None
    return None


def load_trades(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--scenarios", default="new_psar,trend_macd_ema,trend_supertrend")
    ap.add_argument("--profit-pct", type=float, default=4.0)
    ap.add_argument("--check-min", type=int, default=60)
    ap.add_argument("--split", default="test", choices=("test", "train", "both"))
    ap.add_argument("--max-cells", type=int, default=0)
    args = ap.parse_args()

    want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    cells = [c for c in discover_cells(Path(args.cache_dir)) if c["scenario_id"] in want]
    if args.max_cells:
        cells = cells[: args.max_cells]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = HistoricalDatastore(ROOT / "simulation/data/ctengine")
    ohlc_cache: dict[str, pd.DataFrame] = {}
    check_ms = int(args.check_min) * 60 * 1000

    print(
        f"=== ADD hourly >{args.profit_pct:g}% every {args.check_min}m "
        f"(keep original exit otherwise) · cells={len(cells)} · split={args.split} ===",
        flush=True,
    )

    cell_rows: list[dict[str, Any]] = []
    by_pair: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"orig": 0.0, "sim": 0.0, "n": 0, "hourly": 0}
    )
    t0 = time.time()

    for i, cell in enumerate(cells, 1):
        splits = ["train", "test"] if args.split == "both" else [args.split]
        orig_pnls: list[float] = []
        sim_pnls: list[float] = []
        n_hourly = 0
        n_skip = 0
        early_pnls: list[float] = []

        for split in splits:
            path = cell["train_path"] if split == "train" else cell["test_path"]
            for rec in load_trades(Path(path)):
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
                    n_skip += 1
                    continue
                if pair not in ohlc_cache:
                    try:
                        ohlc_cache[pair] = ds.load(pair, "5m")
                    except Exception:
                        ohlc_cache[pair] = pd.DataFrame()

                early = try_hourly_exit(
                    ohlc=ohlc_cache[pair],
                    open_ms=open_ms,
                    close_ms=close_ms if close_ms > open_ms else open_ms + 7 * 86400_000,
                    open_rate=open_rate,
                    is_short=is_short,
                    profit_pct=float(args.profit_pct),
                    check_every_ms=check_ms,
                )
                if early is not None:
                    sim_pnl = float(early["pnl"])
                    n_hourly += 1
                    early_pnls.append(sim_pnl)
                else:
                    sim_pnl = orig

                orig_pnls.append(orig)
                sim_pnls.append(sim_pnl)
                pk = (cell["scenario_id"], pair)
                by_pair[pk]["orig"] += orig
                by_pair[pk]["sim"] += sim_pnl
                by_pair[pk]["n"] += 1
                if early is not None:
                    by_pair[pk]["hourly"] += 1

        o_sum = summarize(orig_pnls)
        s_sum = summarize(sim_pnls)
        e_sum = summarize(early_pnls)
        row = {
            "key": cell["key"],
            "scenario_id": cell["scenario_id"],
            "tp_pct": cell["tp_pct"],
            "sl_pct": cell["sl_pct"],
            "orig": o_sum,
            "sim_hourly": s_sum,
            "early_only": e_sum,
            "delta_pnl": round(s_sum["pnl"] - o_sum["pnl"], 4),
            "n_hourly_exits": n_hourly,
            "hourly_rate": round(n_hourly / max(1, o_sum["n"]), 4),
            "n_skip": n_skip,
        }
        cell_rows.append(row)
        print(
            f"[{i}/{len(cells)}] {cell['key']}: orig={o_sum['pnl']:+.1f} "
            f"sim={s_sum['pnl']:+.1f} d={row['delta_pnl']:+.1f} "
            f"hourly={n_hourly}/{o_sum['n']} ({100 * row['hourly_rate']:.1f}%) "
            f"early_pnl={e_sum['pnl']:+.1f}",
            flush=True,
        )

    by_sc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orig": 0.0, "sim": 0.0, "hourly": 0, "n": 0}
    )
    for r in cell_rows:
        sid = r["scenario_id"]
        by_sc[sid]["orig"] += float(r["orig"]["pnl"])
        by_sc[sid]["sim"] += float(r["sim_hourly"]["pnl"])
        by_sc[sid]["hourly"] += int(r["n_hourly_exits"])
        by_sc[sid]["n"] += int(r["orig"]["n"])

    pair_rows = []
    for (sid, pair), v in by_pair.items():
        pair_rows.append(
            {
                "scenario_id": sid,
                "pair": pair,
                "n": int(v["n"]),
                "orig_pnl": round(v["orig"], 4),
                "sim_pnl": round(v["sim"], 4),
                "delta": round(v["sim"] - v["orig"], 4),
                "hourly_exits": int(v["hourly"]),
            }
        )
    pair_rows.sort(key=lambda x: x["delta"], reverse=True)

    # Best cells where rule helps
    improved = [r for r in cell_rows if r["delta_pnl"] > 0]
    improved.sort(key=lambda r: r["delta_pnl"], reverse=True)

    summary = {
        "updated": datetime.now(tz=UTC).isoformat(),
        "mode": "add_hourly_on_original_trades",
        "note": (
            "At each check_min after entry, if price PnL > profit_pct, close at bar close; "
            "else keep original profit_abs. Early PnL uses stake*lev*(move-2*fee)."
        ),
        "profit_pct": args.profit_pct,
        "check_min": args.check_min,
        "split": args.split,
        "fee": FEE,
        "leverage": LEV,
        "stake": STAKE,
        "n_cells": len(cell_rows),
        "sum_orig_pnl": round(sum(float(r["orig"]["pnl"]) for r in cell_rows), 2),
        "sum_sim_pnl": round(sum(float(r["sim_hourly"]["pnl"]) for r in cell_rows), 2),
        "sum_delta": round(sum(float(r["delta_pnl"]) for r in cell_rows), 2),
        "total_hourly_exits": sum(int(r["n_hourly_exits"]) for r in cell_rows),
        "cells_improved": len(improved),
        "by_scenario": {
            sid: {
                "orig": round(v["orig"], 2),
                "sim": round(v["sim"], 2),
                "delta": round(v["sim"] - v["orig"], 2),
                "hourly_exits": int(v["hourly"]),
                "n": int(v["n"]),
            }
            for sid, v in sorted(by_sc.items())
        },
        "best_delta_cells": improved[:20],
        "worst_delta_cells": sorted(cell_rows, key=lambda r: r["delta_pnl"])[:10],
        "top_pairs_by_delta": pair_rows[:40],
        "cells": cell_rows,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        f"# Add hourly exit: profit > {args.profit_pct:g}% every {args.check_min}m",
        "",
        f"Split `{args.split}` · early fee model stake={STAKE} lev={LEV} fee={FEE}x2",
        "",
        f"- Sum orig: **{summary['sum_orig_pnl']:+.2f}**",
        f"- Sum with rule: **{summary['sum_sim_pnl']:+.2f}**",
        f"- Delta: **{summary['sum_delta']:+.2f}**",
        f"- Hourly early exits: {summary['total_hourly_exits']}",
        f"- Cells improved: {summary['cells_improved']}/{summary['n_cells']}",
        "",
        "## By scenario",
        "",
    ]
    for sid, v in summary["by_scenario"].items():
        md.append(
            f"- `{sid}`: {v['orig']:+.1f} -> {v['sim']:+.1f} "
            f"(d {v['delta']:+.1f}, hourly={v['hourly_exits']}/{v['n']})"
        )
    md.extend(["", "## Top pairs by delta (rule helps)", ""])
    for p in pair_rows[:25]:
        if p["delta"] <= 0:
            continue
        md.append(
            f"- `{p['scenario_id']}` {p['pair']}: d {p['delta']:+.2f} "
            f"(orig {p['orig_pnl']:+.2f} -> {p['sim_pnl']:+.2f}, "
            f"hourly={p['hourly_exits']}/{p['n']})"
        )
    md.extend(["", "## Best cells by delta", ""])
    for r in improved[:15]:
        md.append(
            f"- `{r['key']}`: d {r['delta_pnl']:+.1f} "
            f"({r['orig']['pnl']:+.1f} -> {r['sim_hourly']['pnl']:+.1f}, "
            f"hourly={r['n_hourly_exits']})"
        )
    (out_dir / "comparison.md").write_text("\n".join(md), encoding="utf-8")

    print("\n=== TOTAL ===", flush=True)
    print(
        f"orig={summary['sum_orig_pnl']:+.2f} sim={summary['sum_sim_pnl']:+.2f} "
        f"delta={summary['sum_delta']:+.2f} hourly={summary['total_hourly_exits']} "
        f"improved_cells={summary['cells_improved']}/{summary['n_cells']}",
        flush=True,
    )
    for sid, v in summary["by_scenario"].items():
        print(
            f"  {sid}: {v['orig']:+.1f} -> {v['sim']:+.1f} (d {v['delta']:+.1f}, "
            f"hourly={v['hourly_exits']})",
            flush=True,
        )
    print(f"Saved {out_dir / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
