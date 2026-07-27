#!/usr/bin/env python3
"""Monthly breakdown: no ML vs global ML gate on full_ml_study export."""
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

from simulation.ml.trade_gate import MlEntryGate, load_gate_config
from simulation.scripts.run_full_ml_study import (
    EXPORT_DIR,
    STUDY_DIR,
    _aggregate_mode,
    _load_exported_trades,
    _scenario_dict,
)

OUT_JSON = STUDY_DIR / "global_ml_by_month.json"
OUT_TXT = STUDY_DIR / "razbor_global_ml_po_mesyatsam.txt"

STRATEGY_LABELS = {
    "trend_breakout": "Breakout-Retest",
    "trend_ema": "EMA 50/200",
    "lite_mean_rev": "Mean-reversion",
    "lite_intraday": "Intraday",
    "lite_range": "Range",
    "live_grid": "Grid",
}


def _month_key(rec: dict) -> str:
    ms = int((rec.get("trade") or {}).get("open_ms") or rec.get("open_ms") or 0)
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def _apply_gate(trades: list[dict], gate: MlEntryGate) -> tuple[list[dict], list[dict]]:
    kept, blocked = [], []
    for rec in trades:
        sc = _scenario_dict(rec)
        ml = gate.predict_for_trade(
            sc,
            rec["pair"],
            rec["trade"],
            rec.get("inst_config") or {},
            rec.get("armed_at_ms"),
        )
        row = {**rec, "ml": ml}
        (blocked if gate.should_block(ml, scenario=sc) else kept).append(row)
    return kept, blocked


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.6, help="min_confidence profit_only")
    args = ap.parse_args()

    trades = _load_exported_trades()
    if not trades:
        raise SystemExit(f"no trades in {EXPORT_DIR}")

    cfg = load_gate_config(ROOT)
    for key in ("grid_bots", "strategy_bots", "finder_bots"):
        if key in cfg and isinstance(cfg[key], dict):
            cfg[key] = {**cfg[key], "gate_mode": "profit_only", "min_confidence": args.threshold}

    gate = MlEntryGate(ROOT, model_mode="global")
    gate.config = cfg
    gate.set_enabled(True)
    if not gate._model_ready():
        raise SystemExit("global model not trained — run run_full_ml_study.py --phase train")

    by_month_all: dict[str, list[dict]] = defaultdict(list)
    for rec in trades:
        by_month_all[_month_key(rec)].append(rec)

    months = sorted(by_month_all)
    month_rows: list[dict[str, Any]] = []
    total_no = _aggregate_mode(trades)
    all_kept: list[dict] = []
    all_blocked: list[dict] = []

    print(f"Scoring {len(trades)} trades · global ML · profit >= {args.threshold:.0%}")
    for month in months:
        batch = by_month_all[month]
        kept, blocked = _apply_gate(batch, gate)
        all_kept.extend(kept)
        all_blocked.extend(blocked)
        no_ml = _aggregate_mode(batch)
        with_ml = _aggregate_mode(kept)
        with_ml["blocked"] = len(blocked)
        with_ml["blocked_pnl_usdt"] = round(
            sum(float(r.get("profit_abs") or 0) for r in blocked), 4
        )

        by_strat_no: dict[str, list[dict]] = defaultdict(list)
        by_strat_ml: dict[str, list[dict]] = defaultdict(list)
        for r in batch:
            by_strat_no[r["scenario_id"]].append(r)
        for r in kept:
            by_strat_ml[r["scenario_id"]].append(r)

        row = {
            "month": month,
            "signals": len(batch),
            "no_ml": no_ml,
            "global_ml": with_ml,
            "delta_pnl": round(with_ml["pnl_usdt"] - no_ml["pnl_usdt"], 4),
            "by_strategy": {
                "no_ml": {sid: _aggregate_mode(rows) for sid, rows in sorted(by_strat_no.items())},
                "global_ml": {sid: _aggregate_mode(rows) for sid, rows in sorted(by_strat_ml.items())},
            },
        }
        month_rows.append(row)
        print(
            f"  {month}: no_ml {no_ml['pnl_usdt']:+.2f} -> global {with_ml['pnl_usdt']:+.2f} "
            f"({with_ml['trades']}/{len(batch)} tr, WR {with_ml['win_rate']*100:.1f}%)"
        )

    total_ml = _aggregate_mode(all_kept)
    total_ml["blocked"] = len(all_blocked)
    total_ml["blocked_pnl_usdt"] = round(
        sum(float(r.get("profit_abs") or 0) for r in all_blocked), 4
    )

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "model": "global",
        "gate_mode": "profit_only",
        "min_confidence": args.threshold,
        "n_signals": len(trades),
        "months": month_rows,
        "totals": {"no_ml": total_no, "global_ml": total_ml},
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_txt(payload, OUT_TXT)
    print(f"\nTXT: {OUT_TXT}")
    return 0


def _write_txt(data: dict, path: Path) -> None:
    th = int(float(data["min_confidence"]) * 100)
    lines: list[str] = [
        "=" * 78,
        "  ПОМЕСЯЧНЫЙ РАЗБОР — GLOBAL ML (общая модель)",
        f"  Дата: {data['generated_at'][:10]}",
        f"  Gate: profit_only >= {th}%",
        f"  Сигналов всего: {data['n_signals']}",
        "=" * 78,
        "",
        "1. ИТОГО ПО ПЕРИОДУ",
        "-" * 78,
    ]
    no = data["totals"]["no_ml"]
    ml = data["totals"]["global_ml"]
    lines += [
        f"  Без ML:      {no['trades']:5} сд.  {no['pnl_usdt']:+10.2f} USDT  WR {no['win_rate']*100:.1f}%",
        f"  Global ML:   {ml['trades']:5} сд.  {ml['pnl_usdt']:+10.2f} USDT  WR {ml['win_rate']*100:.1f}%  "
        f"(заблок. {ml.get('blocked', 0)}, их PnL {ml.get('blocked_pnl_usdt', 0):+.2f})",
        f"  Прирост ML:  {ml['pnl_usdt'] - no['pnl_usdt']:+.2f} USDT",
        "",
        "2. ПО МЕСЯЦАМ (сводная таблица)",
        "-" * 78,
        f"  {'Месяц':<8} {'Сигн.':>6} {'Без ML':>10} {'Global ML':>10} {'Δ PnL':>9} "
        f"{'ML сд.':>6} {'WR%':>6}",
    ]
    for row in data["months"]:
        m = row["month"]
        n = row["no_ml"]
        g = row["global_ml"]
        lines.append(
            f"  {m:<8} {row['signals']:6} {n['pnl_usdt']:+10.2f} {g['pnl_usdt']:+10.2f} "
            f"{row['delta_pnl']:+9.2f} {g['trades']:6} {g['win_rate']*100:5.1f}%"
        )
    lines += ["", "3. ДЕТАЛИ ПО КАЖДОМУ МЕСЯЦУ", "-" * 78]
    for row in data["months"]:
        m = row["month"]
        n, g = row["no_ml"], row["global_ml"]
        lines += [
            "",
            f"  === {m} ===  сигналов: {row['signals']}",
            f"  Без ML:    {n['trades']} сд.  {n['pnl_usdt']:+.2f} USDT  "
            f"W{n['wins']} L{n['losses']}  WR {n['win_rate']*100:.1f}%",
            f"  Global ML: {g['trades']} сд.  {g['pnl_usdt']:+.2f} USDT  "
            f"W{g['wins']} L{g['losses']}  WR {g['win_rate']*100:.1f}%  "
            f"заблок. {g.get('blocked', 0)} ({g.get('blocked_pnl_usdt', 0):+.2f} USDT)",
            "  По стратегиям (global ML):",
        ]
        for sid, agg in sorted(row["by_strategy"]["global_ml"].items()):
            label = STRATEGY_LABELS.get(sid, sid)
            lines.append(
                f"    {label:<18} {agg['trades']:4} сд.  {agg['pnl_usdt']:+8.2f} USDT  "
                f"WR {agg['win_rate']*100:.1f}%"
            )
        no_strats = row["by_strategy"]["no_ml"]
        missing = [sid for sid in no_strats if sid not in row["by_strategy"]["global_ml"]]
        if missing:
            lines.append("  Стратегии без проходов ML в этом месяце:")
            for sid in missing:
                label = STRATEGY_LABELS.get(sid, sid)
                agg = no_strats[sid]
                lines.append(
                    f"    {label:<18} (было {agg['trades']} сд., {agg['pnl_usdt']:+.2f} без ML)"
                )

    # best / worst months
    by_delta = sorted(data["months"], key=lambda r: r["delta_pnl"], reverse=True)
    by_ml_pnl = sorted(data["months"], key=lambda r: r["global_ml"]["pnl_usdt"], reverse=True)
    lines += [
        "",
        "4. ВЫВОДЫ",
        "-" * 78,
        f"  Лучший месяц по PnL (global ML): {by_ml_pnl[0]['month']} "
        f"({by_ml_pnl[0]['global_ml']['pnl_usdt']:+.2f} USDT)",
        f"  Худший месяц по PnL (global ML): {by_ml_pnl[-1]['month']} "
        f"({by_ml_pnl[-1]['global_ml']['pnl_usdt']:+.2f} USDT)",
        f"  Наибольший прирост от ML: {by_delta[0]['month']} (Δ {by_delta[0]['delta_pnl']:+.2f} USDT)",
    ]
    neg_ml = [r for r in data["months"] if r["global_ml"]["pnl_usdt"] < 0]
    if neg_ml:
        lines.append(f"  Месяцев с минусом при global ML: {len(neg_ml)} — "
                     + ", ".join(r["month"] for r in neg_ml))
    else:
        lines.append("  Все месяцы с положительным PnL при global ML.")
    lines += ["", "=" * 78, "  Конец отчёта", "=" * 78, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
