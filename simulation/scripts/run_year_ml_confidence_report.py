#!/usr/bin/env python3
"""Year sim: scanner vs ML gate + ML confidence distribution (prod bots)."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.trade_gate import _resolve_gate_rules, load_gate_config  # noqa: E402
from simulation.scripts.apply_prod_ml_config import main as apply_prod  # noqa: E402
from simulation.scripts.compare_ml_by_bot_month import (  # noqa: E402
    MONTHS,
    aggregate_instances,
    build_bot_totals,
    print_bot_table,
    run_month_with_bots,
)
from simulation.scripts.comparison_common import period_months  # noqa: E402
from simulation.scripts.run_prod_may_june_report import (  # noqa: E402
    PROD_CFG,
    _sum_bots,
    filter_row,
)

OUT_PATH = ROOT / "simulation/results/year_ml_confidence_report.json"
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _trade_key(t: dict) -> tuple:
    return (t.get("open_ms"), t.get("pair"), bool(t.get("is_short")))


def collect_scored_trades(instances: list[dict], enabled: set[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for inst in instances:
        sid = inst.get("scenario_id")
        if sid not in enabled:
            continue
        for t in (inst.get("trades") or []) + (inst.get("ml_skipped_trades") or []):
            ml = t.get("ml")
            if not ml:
                continue
            k = _trade_key(t)
            if k in seen:
                continue
            seen.add(k)
            out.append({**t, "scenario_id": sid, "label": inst.get("label") or sid})
    return out


def ml_would_block(ml: dict, scenario_id: str, gate_cfg: dict) -> bool:
    scenario = {"id": scenario_id, "scenario_id": scenario_id}
    mode, min_conf = _resolve_gate_rules(gate_cfg, scenario)
    predicted = ml.get("predicted")
    conf = float(ml.get("confidence") or 0.0)
    if mode == "profit_only":
        if predicted != "profit":
            return True
        profit_conf = float(ml.get("confidence_profit") or (conf if predicted == "profit" else 0.0))
        return profit_conf < min_conf
    block = gate_cfg.get("block_predicted", "loss")
    if predicted != block:
        return False
    return conf >= min_conf


def confidence_histogram(scored: list[dict]) -> dict[str, Any]:
    profit_confs = [float(t["ml"]["confidence_profit"]) for t in scored if t.get("ml")]
    if not profit_confs:
        return {"total": 0, "thresholds": {}, "predicted_profit_pct": 0.0, "avg_profit_conf": 0.0}

    n = len(profit_confs)
    pred_profit = sum(1 for t in scored if (t.get("ml") or {}).get("predicted") == "profit")
    thresholds = {}
    for th in THRESHOLDS:
        count = sum(1 for c in profit_confs if c >= th)
        thresholds[f">={int(th * 100)}%"] = {
            "count": count,
            "pct_of_signals": round(100 * count / n, 2),
        }
    return {
        "total": n,
        "predicted_profit_pct": round(100 * pred_profit / n, 2),
        "avg_profit_conf_pct": round(100 * sum(profit_confs) / n, 2),
        "median_profit_conf_pct": round(100 * sorted(profit_confs)[n // 2], 2),
        "max_profit_conf_pct": round(100 * max(profit_confs), 2),
        "thresholds": thresholds,
    }


def histogram_by_bot(scored: list[dict]) -> dict[str, Any]:
    by_bot: dict[str, list[dict]] = defaultdict(list)
    for t in scored:
        by_bot[t["scenario_id"]].append(t)
    return {sid: confidence_histogram(rows) for sid, rows in sorted(by_bot.items())}


def simulate_at_threshold(scored: list[dict], gate_cfg: dict, min_conf: float) -> dict[str, Any]:
    cfg = json.loads(json.dumps(gate_cfg))
    for key in ("grid_bots", "strategy_bots", "finder_bots"):
        if key in cfg and isinstance(cfg[key], dict):
            cfg[key] = {**cfg[key], "gate_mode": "profit_only", "min_confidence": min_conf}

    kept: list[dict] = []
    blocked: list[dict] = []
    for t in scored:
        if ml_would_block(t["ml"], t["scenario_id"], cfg):
            blocked.append(t)
        else:
            kept.append(t)

    pnl_kept = sum(float(t.get("profit_abs") or 0) for t in kept)
    pnl_blocked = sum(float(t.get("profit_abs") or 0) for t in blocked)
    wins_kept = sum(1 for t in kept if float(t.get("profit_abs") or 0) >= 0)
    wins_blocked = sum(1 for t in blocked if float(t.get("profit_abs") or 0) >= 0)
    return {
        "min_confidence": min_conf,
        "trades": len(kept),
        "blocked": len(blocked),
        "pnl_usdt": round(pnl_kept, 4),
        "blocked_pnl_usdt": round(pnl_blocked, 4),
        "win_rate": round(wins_kept / len(kept), 4) if kept else 0,
        "blocked_win_rate": round(wins_blocked / len(blocked), 4) if blocked else 0,
    }


def run_month_collect(year: int, month: int, workers: int, enabled: set[str]) -> tuple[dict, list[dict]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from simulation.exchange_sim.bot_session import BotSessionManager
    from simulation.scripts.compare_ml_by_month import month_range
    from simulation.scripts.select_player_pairs import pool_pairs

    start_ms, end_ms, label = month_range(year, month)
    datadir = ROOT / "simulation/data/freqtrade"
    pairs = pool_pairs(ROOT)
    mgr = BotSessionManager(ROOT)
    gate = mgr._get_ml_gate()
    gate.set_enabled(False)
    mgr.init_live_session(pairs, start_ms, end_ms, datadir)
    pool = mgr.status.get("sim_pool") or pairs
    order = [s for s in mgr.enabled_scenario_ids() if s in enabled]

    def load_one(sid: str) -> str:
        mgr.load_scenario_instances(sid, pool, start_ms, end_ms, datadir)
        return sid

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(load_one, sid): sid for sid in order}
        for fut in as_completed(futs):
            fut.result()

    instances = [i for i in mgr.instances if i.get("scenario_id") in enabled]
    scored = collect_scored_trades(mgr.instances, enabled)
    row = filter_row(
        {
            "month": label,
            "without_ml": aggregate_instances(instances, use_ml=False),
            "with_ml": aggregate_instances(instances, use_ml=True),
            "bots": [],
        },
        enabled,
    )
    from simulation.scripts.compare_ml_by_bot_month import bots_from_month

    row["bots"] = [b for b in bots_from_month(instances) if b["scenario_id"] in enabled]
    row["without_ml"] = _sum_bots(row["bots"], use_ml=False)
    row["with_ml"] = _sum_bots(row["bots"], use_ml=True)
    row["delta_pnl_usdt"] = round(row["with_ml"]["pnl_usdt"] - row["without_ml"]["pnl_usdt"], 4)
    return row, scored


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Year ML confidence + with/without ML report")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--from-month", default="2025-11")
    ap.add_argument("--to-month", default="2026-06")
    ap.add_argument("--skip-apply", action="store_true")
    args = ap.parse_args()

    if not args.skip_apply:
        apply_prod()

    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled = set(prod["enabled_scenarios"])
    ml_gate = prod.get("ml_gate") or load_gate_config(ROOT)

    y0, m0 = map(int, args.from_month.split("-"))
    y1, m1 = map(int, args.to_month.split("-"))
    months = [(y, m) for y, m in period_months(y0, m0, y1, m1) if (y, m) >= (y0, m0) and (y, m) <= (y1, m1)]
    if not months:
        months = list(MONTHS)

    print(f"=== Year sim ML confidence ({args.from_month} — {args.to_month}) ===")
    print(f"Bots: {', '.join(sorted(enabled))}")
    print(f"ML gate: {json.dumps(ml_gate, ensure_ascii=False)}")

    month_rows: list[dict] = []
    all_scored: list[dict] = []

    for y, m in months:
        label = f"{y}-{m:02d}"
        print(f"\n  running {label}...", flush=True)
        row, scored = run_month_collect(y, m, args.workers, enabled)
        month_rows.append(row)
        all_scored.extend(scored)
        print_bot_table(row["month"], row["bots"])

    hist = confidence_histogram(all_scored)
    by_bot = histogram_by_bot(all_scored)

    total_no_ml = round(sum(r["without_ml"]["pnl_usdt"] for r in month_rows), 4)
    total_ml = round(sum(r["with_ml"]["pnl_usdt"] for r in month_rows), 4)
    trades_no = sum(r["without_ml"]["trades"] for r in month_rows)
    trades_ml = sum(r["with_ml"]["trades"] for r in month_rows)
    skipped = sum(r["with_ml"]["ml_skipped"] for r in month_rows)

    threshold_sweep = [simulate_at_threshold(all_scored, ml_gate, th) for th in THRESHOLDS]
    threshold_sweep.append({"min_confidence": 0.0, "trades": len(all_scored), "blocked": 0, "note": "no gate"})

    bot_totals = [b for b in build_bot_totals(month_rows) if b["scenario_id"] in enabled]

    print("\n=== PORTFOLIO (scanner vs current ML gate) ===")
    print(f"  Без ML:  {trades_no} сделок · {total_no_ml:+.2f} USDT")
    print(f"  С ML:    {trades_ml} сделок · {total_ml:+.2f} USDT (пропущено {skipped})")
    print(f"  Дельта:  {total_ml - total_no_ml:+.2f} USDT")

    print("\n=== ML confidence (все сигналы сканера, n={}) ===".format(hist["total"]))
    print(f"  predicted=profit: {hist.get('predicted_profit_pct', 0):.1f}%")
    print(f"  avg profit conf:  {hist.get('avg_profit_conf_pct', 0):.1f}%")
    print(f"  median:           {hist.get('median_profit_conf_pct', 0):.1f}%")
    print(f"  max:              {hist.get('max_profit_conf_pct', 0):.1f}%")
    print("\n  Доля сигналов с profit confidence:")
    for label, row in (hist.get("thresholds") or {}).items():
        print(f"    {label}: {row['count']} ({row['pct_of_signals']:.1f}%)")

    print("\n=== Sweep profit_only (симуляция порога) ===")
    print(f"{'Порог':>8} {'Сделок':>8} {'Блок':>8} {'PnL':>12} {'WR':>8}")
    for row in threshold_sweep:
        if row.get("note"):
            continue
        print(
            f"{int(row['min_confidence'] * 100):>7}% "
            f"{row['trades']:>8} "
            f"{row['blocked']:>8} "
            f"{row['pnl_usdt']:>+12.2f} "
            f"{row.get('win_rate', 0) * 100:>7.1f}%"
        )

    out = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "period": f"{args.from_month} — {args.to_month}",
        "bots": sorted(enabled),
        "ml_gate": ml_gate,
        "months": month_rows,
        "bot_totals": bot_totals,
        "portfolio": {
            "without_ml": {"trades": trades_no, "pnl_usdt": total_no_ml},
            "with_ml": {"trades": trades_ml, "pnl_usdt": total_ml, "skipped": skipped},
            "delta_pnl_usdt": round(total_ml - total_no_ml, 4),
        },
        "confidence": hist,
        "confidence_by_bot": by_bot,
        "threshold_sweep": threshold_sweep,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
