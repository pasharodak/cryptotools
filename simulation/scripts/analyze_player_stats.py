#!/usr/bin/env python3
"""Analyze player backtest exports: per-pair PnL, profit/loss columns, totals."""
from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_trades_from_zip(zpath: Path) -> tuple[str, list[dict]]:
    with zipfile.ZipFile(zpath) as z:
        js = next(n for n in z.namelist() if n.endswith(".json") and "_config" not in n)
        data = json.loads(z.read(js))
    strat_block = data.get("strategy") or {}
    if len(strat_block) == 1:
        strat = next(iter(strat_block))
        return strat, strat_block[strat].get("trades") or []
    comp = data.get("strategy_comparison") or []
    if comp:
        return comp[0]["key"], []
    return "unknown", []


def summarize(trades: list[dict]) -> dict:
    by_pair: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "pnl": 0.0, "profit": 0.0, "loss": 0.0, "w": 0, "l": 0}
    )
    for t in trades:
        sym = t["pair"].split("/")[0]
        row = by_pair[sym]
        row["n"] += 1
        p = float(t["profit_abs"])
        row["pnl"] += p
        if p >= 0:
            row["w"] += 1
            row["profit"] += p
        else:
            row["l"] += 1
            row["loss"] += p
    gross_profit = sum(r["profit"] for r in by_pair.values())
    gross_loss = sum(r["loss"] for r in by_pair.values())
    wins = sum(r["w"] for r in by_pair.values())
    losses = sum(r["l"] for r in by_pair.values())
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "profit": gross_profit,
        "loss": gross_loss,
        "pnl": gross_profit + gross_loss,
        "win_rate": (wins / len(trades) * 100) if trades else 0,
        "pairs": dict(sorted(by_pair.items(), key=lambda x: x[1]["pnl"], reverse=True)),
    }


def main() -> int:
    bt_dir = root() / "simulation" / "results" / "player_backtests"
    zips = sorted(bt_dir.glob("backtest-result-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    seen: set[str] = set()
    reports: dict[str, dict] = {}
    for zpath in zips:
        strat, trades = load_trades_from_zip(zpath)
        if strat in seen:
            continue
        seen.add(strat)
        reports[strat] = {"file": zpath.name, **summarize(trades)}
        if len(seen) >= 2:
            break

    combined = {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "loss": 0.0, "pnl": 0.0}
    for rep in reports.values():
        for k in ("trades", "wins", "losses", "profit", "loss", "pnl"):
            combined[k] += rep[k]

    out = {
        "timerange": json.loads((root() / "simulation" / "config" / "manifest.json").read_text())[
            "player_timerange"
        ],
        "scenarios": reports,
        "combined": combined,
    }
    out_path = root() / "simulation" / "results" / "player_stats.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"=== Combined ({out['timerange']}) ===")
    c = combined
    print(
        f"  {c['trades']} trades | profit {c['profit']:+.4f} | loss {c['loss']:+.4f} | "
        f"net {c['pnl']:+.4f} USDT"
    )
    for strat, rep in reports.items():
        print(f"\n--- {strat} ({rep['file']}) ---")
        print(
            f"  {rep['trades']} sd | +{rep['profit']:.4f} {rep['loss']:.4f} | "
            f"net {rep['pnl']:+.4f} | WR {rep['win_rate']:.0f}%"
        )
        for sym, s in rep["pairs"].items():
            print(
                f"  {sym:6} n={s['n']:3} profit={s['profit']:+.4f} loss={s['loss']:+.4f} net={s['pnl']:+.4f}"
            )
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
