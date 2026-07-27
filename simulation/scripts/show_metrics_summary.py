#!/usr/bin/env python3
"""Print current ML study metrics snapshot."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    state_path = ROOT / "simulation/results/full_ml_study/export_state.json"
    exp = ROOT / "simulation/results/full_ml_study/export"
    cmp_path = ROOT / "simulation/results/full_ml_study/comparison.json"
    train_path = ROOT / "simulation/results/full_ml_study/training.json"
    grid_path = ROOT / "simulation/results/grid_ml/export_state.json"

    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        months = state.get("completed_months") or []
        tot = int(state.get("total_trades") or 0)
        w, l = int(state.get("wins") or 0), int(state.get("losses") or 0)
        print("=== PIPELINE EXPORT (in progress) ===")
        print(f"Months: {len(months)}/19 — latest {months[-1] if months else '—'}")
        if tot:
            print(f"Trades: {tot} · W{w} L{l} · WR {w/tot*100:.1f}%")
        print()

    by: dict[str, dict] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    if exp.is_dir():
        for p in exp.rglob("*.jsonl"):
            for line in p.open(encoding="utf-8"):
                if not line.strip():
                    continue
                r = json.loads(line)
                sid = r.get("scenario_id") or "?"
                pnl = float(r.get("profit_abs") or 0)
                by[sid]["n"] += 1
                by[sid]["pnl"] += pnl
                if pnl >= 0:
                    by[sid]["w"] += 1
    if by:
        print("=== EXPORT by strategy (partial/full) ===")
        net = 0.0
        for sid, v in sorted(by.items(), key=lambda x: -x[1]["n"]):
            wr = v["w"] / v["n"] * 100 if v["n"] else 0
            net += v["pnl"]
            print(f"  {sid:<18} {v['n']:>5} tr  WR {wr:5.1f}%  PnL {v['pnl']:+9.2f}")
        print(f"  {'TOTAL':<18} {sum(x['n'] for x in by.values()):>5} tr           PnL {net:+9.2f}")
        print()

    if cmp_path.is_file():
        cmp = json.loads(cmp_path.read_text(encoding="utf-8"))
        p = cmp.get("portfolio") or {}
        print(f"=== OOS COMPARE ({cmp.get('compared_at', '')[:10]}) ===")
        for key, label in (
            ("no_ml", "No ML"),
            ("global_ml", "Global ML"),
            ("per_strategy_ml", "Per-strat ML"),
        ):
            row = p.get(key) or {}
            print(
                f"  {label:<12} {row.get('trades', 0):>4} tr  "
                f"{row.get('pnl_usdt', 0):>+8.2f} USDT  WR {row.get('win_rate', 0)*100:.1f}%"
            )
        print()

    if train_path.is_file():
        tr = json.loads(train_path.read_text(encoding="utf-8")).get("global") or {}
        print(f"=== GLOBAL MODEL ({str(tr.get('trained_at', ''))[:10]}) ===")
        print(
            f"  n={tr.get('n_trades')} acc={tr.get('accuracy', 0)*100:.1f}% "
            f"ROC={tr.get('roc_auc', 0):.3f} profR={tr.get('profit_recall', 0)*100:.1f}% "
            f"lossR={tr.get('loss_recall', 0)*100:.1f}%"
        )
        print()

    if grid_path.is_file():
        g = json.loads(grid_path.read_text(encoding="utf-8"))
        t = int(g.get("total_trades") or 0)
        gw, gl = int(g.get("wins") or 0), int(g.get("losses") or 0)
        print("=== GRID prod-parity export ===")
        print(f"  {t} tr · W{gw} L{gl} · WR {gw/t*100:.1f}%" if t else "  (empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
