#!/usr/bin/env python3
"""Write comprehensive TXT report from full ML study (export, train, compare)."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.scripts.run_full_ml_study import (  # noqa: E402
    COMPARE_PATH,
    EXPORT_DIR,
    MODEL_DIR,
    STUDY_DIR,
    _load_exported_trades,
)

OUT_TXT = STUDY_DIR / "full_ml_study_report.txt"
GRID_META = ROOT / "simulation/results/grid_ml/dataset_stats.json"
GRID_MODEL_META = ROOT / "user_data/models/pnl_classifier/by_scenario/live_grid/pnl_classifier_meta.json"
PROD_PAIRS = ROOT / "simulation/config/prod_pairs_200.json"

STRATEGY_LABELS = {
    "trend_breakout": "Breakout-Retest",
    "trend_ema": "EMA 50/200",
    "lite_mean_rev": "Mean-reversion (BB)",
    "lite_intraday": "Intraday",
    "lite_range": "Range",
    "live_grid": "Grid (VolatilityGrid)",
}


def _load_json(path: Path) -> dict | None:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _fmt_agg(name: str, agg: dict) -> str:
    blocked = agg.get("blocked")
    extra = ""
    if blocked is not None:
        extra = f"  заблок. {blocked} ({agg.get('blocked_pnl_usdt', 0):+.2f} USDT)"
    return (
        f"  {name:<14} {agg.get('trades', 0):5} сд.  {agg.get('pnl_usdt', 0):+10.2f} USDT  "
        f"W{agg.get('wins', 0)} L{agg.get('losses', 0)}  WR {(agg.get('win_rate') or 0)*100:5.1f}%{extra}"
    )


def _model_block(meta: dict | None, title: str) -> list[str]:
    if not meta:
        return [f"  {title}: нет данных", ""]
    lines = [
        f"  {title}:",
        f"    сделок {meta.get('n_trades', '?')} (train {meta.get('n_train', '?')}, test {meta.get('n_test', '?')})",
        f"    accuracy {float(meta.get('accuracy') or 0)*100:.1f}%  ROC-AUC {meta.get('roc_auc')}  "
        f"profit_recall {float(meta.get('profit_recall') or 0)*100:.1f}%  "
        f"loss_recall {float(meta.get('loss_recall') or 0)*100:.1f}%",
    ]
    if meta.get("skipped"):
        lines.append(f"    ПРОПУЩЕНО: {meta.get('reason', meta.get('skipped'))}")
    return lines + [""]


def build_report(root: Path) -> str:
    compare = _load_json(COMPARE_PATH) or {}
    training = _load_json(STUDY_DIR / "training.json") or {}
    export_index = _load_json(EXPORT_DIR / "index.json") or {}
    global_meta = _load_json(MODEL_DIR / "pnl_classifier_meta.json") or training.get("global") or {}
    per_summary = _load_json(MODEL_DIR / "by_scenario/training_summary.json") or training.get("per_scenario") or {}
    grid_meta = _load_json(GRID_MODEL_META)
    grid_stats = _load_json(GRID_META)
    prod_pairs = _load_json(PROD_PAIRS)
    gate = compare.get("gate") or _load_json(root / "user_data/ml_entry_gate.json") or {}

    trades = _load_exported_trades()
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "=" * 80,
        "  ПОЛНЫЙ ОТЧЁТ ML — PROD 200 ПАР · СИМУЛЯЦИЯ · ОБУЧЕНИЕ · СРАВНЕНИЕ",
        f"  Сгенерировано: {now}",
        "=" * 80,
        "",
        "1. ДАТАСЕТ И ПАРЫ",
        "-" * 80,
    ]

    if prod_pairs:
        lines.append(f"  Prod-пары (Bybit volume): {len(prod_pairs.get('pairs') or [])}")
        lines.append(f"  Обновлено: {str(prod_pairs.get('fetched_at', ''))[:19]}")
    if export_index:
        lines += [
            f"  Период экспорта: {export_index.get('timerange', '?')}",
            f"  Пар в симе: {export_index.get('pairs', '?')} ({export_index.get('pairs_mode', '?')})",
            f"  Сигналов scanner: {export_index.get('total_trades', len(trades))}",
            f"  Wins / Losses: {export_index.get('wins', '?')} / {export_index.get('losses', '?')}",
        ]
    else:
        lines.append(f"  Сигналов в export: {len(trades)}")
    lines.append("")

    lines += ["2. КОНФИГ ML GATE (prod)", "-" * 80]
    for key in ("finder_bots", "strategy_bots", "grid_bots"):
        sub = gate.get(key) or {}
        if sub:
            th = int(float(sub.get("min_confidence", 0)) * 100)
            lines.append(f"  {key}: {sub.get('gate_mode', '?')} >= {th}%")
    lines.append("")

    lines += [
        "3. СРАВНЕНИЕ ПОРТФЕЛЯ — OOS test (новейшие 20%, честные цифры)",
        "-" * 80,
        "  Метод: export без ML gate → gate применяется только на test-split (модель не видела эти сделки).",
        f"  Сигналов всего: {compare.get('n_signals', '?')} · OOS test: {compare.get('n_signals_oos', '?')}",
        "",
    ]
    portfolio = compare.get("portfolio") or {}
    for mode, label in (
        ("no_ml", "Без ML"),
        ("global_ml", "Global ML"),
        ("per_strategy_ml", "Per-strategy ML"),
    ):
        agg = portfolio.get(mode) or {}
        lines.append(_fmt_agg(label, agg))
    if portfolio.get("global_ml") and portfolio.get("no_ml"):
        delta = float(portfolio["global_ml"].get("pnl_usdt") or 0) - float(
            portfolio["no_ml"].get("pnl_usdt") or 0
        )
        lines.append(f"  Прирост Global ML (OOS): {delta:+.2f} USDT")
    biased = compare.get("portfolio_all_data") or {}
    if biased.get("global_ml") and biased.get("no_ml"):
        delta_biased = float(biased["global_ml"].get("pnl_usdt") or 0) - float(
            biased["no_ml"].get("pnl_usdt") or 0
        )
        lines.append(
            f"  ⚠ In-sample (вся история, ЗАВЫШЕНО — не использовать): "
            f"Global ML {biased['global_ml'].get('pnl_usdt'):+.2f} vs no ML {biased['no_ml'].get('pnl_usdt'):+.2f} "
            f"(Δ {delta_biased:+.2f})"
        )
    lines.append("")

    lines += ["4. ПО СТРАТЕГИЯМ (сделки / PnL / WR)", "-" * 80]
    by_strat = compare.get("by_strategy") or {}
    sids = sorted(set((by_strat.get("no_ml") or {}) | (by_strat.get("global_ml") or {})))
    header = f"  {'Стратегия':<22} {'No ML':>12} {'Global ML':>12} {'Per-strat ML':>12} {'dPnL':>9}"
    lines.append(header)
    for sid in sids:
        label = STRATEGY_LABELS.get(sid, sid)[:22]
        no = (by_strat.get("no_ml") or {}).get(sid) or {}
        gl = (by_strat.get("global_ml") or {}).get(sid) or {}
        ps = (by_strat.get("per_strategy_ml") or {}).get(sid) or {}
        d = float(gl.get("pnl_usdt") or 0) - float(no.get("pnl_usdt") or 0)
        lines.append(
            f"  {label:<22} {no.get('pnl_usdt', 0):+12.2f} {gl.get('pnl_usdt', 0):+12.2f} "
            f"{ps.get('pnl_usdt', 0):+12.2f} {d:+9.2f}"
        )
        lines.append(
            f"  {'':22} {no.get('trades', 0):5} сд WR{(no.get('win_rate') or 0)*100:4.0f}%   "
            f"{gl.get('trades', 0):5} сд WR{(gl.get('win_rate') or 0)*100:4.0f}%   "
            f"{ps.get('trades', 0):5} сд WR{(ps.get('win_rate') or 0)*100:4.0f}%"
        )
    lines.append("")

    conf = compare.get("confidence") or {}
    if conf:
        lines += [
            "5. РАСПРЕДЕЛЕНИЕ CONFIDENCE (global model, OOS test only)",
            "-" * 80,
            f"  Средняя confidence profit: {conf.get('avg_profit_pct', 0):.1f}%",
            f"  Сигналов >= 80%: {conf.get('count_ge_80', 0)} ({conf.get('pct_ge_80', 0):.1f}%)",
        ]
        for th, row in (conf.get("thresholds") or {}).items():
            lines.append(f"  {th}: {row.get('count', 0)} ({row.get('pct', 0):.1f}%)")
        lines.append("")

    lines += ["6. ОБУЧЕННЫЕ МОДЕЛИ", "-" * 80]
    lines += _model_block(global_meta, "Global (pnl_classifier.joblib)")

    results = per_summary.get("results") or {}
    lines.append("  Per-strategy:")
    for sid in sorted(per_summary.get("scenarios") or results.keys()):
        meta = results.get(sid)
        if not meta:
            continue
        label = STRATEGY_LABELS.get(sid, sid)
        if meta.get("skipped"):
            lines.append(f"    {label}: SKIP — {meta.get('reason', '?')} (n={meta.get('n_trades', 0)})")
        else:
            lines.append(
                f"    {label}: n={meta.get('n_trades')} acc={float(meta.get('accuracy') or 0)*100:.1f}% "
                f"ROC={meta.get('roc_auc')} profR={float(meta.get('profit_recall') or 0)*100:.0f}%"
            )
    lines.append("")

    if grid_stats or grid_meta:
        lines += ["7. GRID BOT (live_grid) — отдельная модель", "-" * 80]
        if grid_stats:
            lines.append(
                f"  Dataset: {grid_stats.get('n_trades')} сд. W{grid_stats.get('wins')} L{grid_stats.get('losses')} "
                f"WR {float(grid_stats.get('win_rate') or 0)*100:.1f}%"
            )
        lines += _model_block(grid_meta, "Grid model")
        grid_no = (by_strat.get("no_ml") or {}).get("live_grid") or {}
        grid_gl = (by_strat.get("global_ml") or {}).get("live_grid") or {}
        grid_ps = (by_strat.get("per_strategy_ml") or {}).get("live_grid") or {}
        if grid_no:
            lines.append(_fmt_agg("Grid без ML", grid_no))
            lines.append(_fmt_agg("Grid global", grid_gl))
            lines.append(_fmt_agg("Grid per-strat", grid_ps))
        lines.append("")

    lines += [
        "8. ФАЙЛЫ",
        "-" * 80,
        f"  Export:     {EXPORT_DIR}",
        f"  Compare:    {COMPARE_PATH}",
        f"  Training:   {STUDY_DIR / 'training.json'}",
        f"  Global:     {MODEL_DIR / 'pnl_classifier.joblib'}",
        f"  Per-scen:   {MODEL_DIR / 'by_scenario/'}",
        f"  Grid live:  {GRID_MODEL_META.parent}",
        "=" * 80,
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    text = build_report(ROOT)
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text(text, encoding="utf-8")
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))
    print(f"Saved: {OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
