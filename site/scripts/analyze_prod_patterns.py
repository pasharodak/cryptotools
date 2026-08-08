#!/usr/bin/env python3
"""Deep pattern analysis of prod trades + optional sim backtest on grid pairs."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_JSON = ROOT / "simulation/results/prod_trades_analysis_20260705_20260706.json"
OUT_TXT = ROOT / "simulation/results/prod_patterns_report.txt"
OUT_JSON = ROOT / "simulation/results/prod_patterns_analysis.json"


def conf(t: dict) -> float | None:
    m = t.get("ml") or {}
    v = m.get("ml_gate_confidence") or m.get("ml_confidence")
    return float(v) if v is not None else None


def hour_utc(t: dict) -> int:
    ts = t.get("close_date") or t.get("open_date") or ""
    try:
        return int(ts[11:13])
    except (ValueError, TypeError):
        return -1


def enrich_market(trades: list[dict]) -> list[dict]:
    from simulation.ml.pnl_classifier import make_market_store, trade_to_features
    from simulation.ml.pnl_classifier import export_record_to_train_rec

    store = make_market_store(ROOT)
    out = []
    for t in trades:
        if t.get("is_open"):
            continue
        rec = {
            "scenario_id": "live_grid" if t["bot"] == "Grid" else "strategy",
            "pair": t["pair"],
            "profit_abs": t["close_profit_abs"],
            "trade": {"open_ms": _parse_ms(t.get("open_date")), "is_short": t.get("is_short")},
            "basis": {"timeframe": "5m"},
        }
        feats = trade_to_features(rec)
        mkt = store.features_at(t["pair"], feats["open_ms"], "5m")
        row = {**t, "market": mkt, "hour_utc": hour_utc(t)}
        out.append(row)
    return out


def _parse_ms(s: str | None) -> int:
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def bucket_stats(rows: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(key_fn(r))].append(r)
    out = {}
    for k, items in sorted(groups.items(), key=lambda x: sum(float(t["close_profit_abs"] or 0) for t in x[1])):
        pnl = sum(float(t["close_profit_abs"] or 0) for t in items)
        w = sum(1 for t in items if float(t["close_profit"] or 0) > 0)
        out[k] = {"n": len(items), "pnl": round(pnl, 4), "w": w, "l": len(items) - w, "wr": round(w / len(items), 3) if items else 0}
    return out


def avg_market(rows: list[dict], field: str) -> float | None:
    vals = [r["market"].get(field) for r in rows if r.get("market") and r["market"].get(field) is not None]
    return round(mean(vals), 4) if vals else None


def run_grid_backtest(pairs: list[str], timerange: str, strategy: str, config: str) -> dict[str, Any]:
    from simulation.exchange_sim.bot_session import parse_backtest_pnl, patch_whitelist

    ft = ROOT / ".venv/Scripts/ctbot.exe"
    runtime = ROOT / "simulation/data/runtime/grid_pattern_test.json"
    cfg = patch_whitelist(ROOT, config, pairs, runtime)
    cfg["pairlists"] = [{"method": "StaticPairList"}]
    cfg["strategy"] = strategy
    if strategy != "VolatilityGridStrategy":
        cfg["strategy_path"] = "simulation/strategies"
    runtime.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    strat_path = (
        str(ROOT / "user_data/strategies")
        if strategy == "VolatilityGridStrategy"
        else str(ROOT / "simulation/strategies")
    )
    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(runtime),
        "--strategy",
        strategy,
        "--strategy-path",
        strat_path,
        "--datadir",
        str(ROOT / "simulation/data/ctengine"),
        "--timerange",
        timerange,
        "--timeframe",
        "5m",
        "--trading-mode",
        "futures",
        "--cache",
        "day",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    text = proc.stdout + proc.stderr
    parsed = parse_backtest_pnl(text)
    return {
        "strategy": strategy,
        "pairs": len(pairs),
        "timerange": timerange,
        "trades": parsed.get("total_trades", 0),
        "pnl_usdt": parsed.get("profit_abs", 0),
        "rc": proc.returncode,
    }


def analyze() -> dict[str, Any]:
    data = json.loads(PROD_JSON.read_text(encoding="utf-8"))
    trades = [t for t in data["trades"] if not t["is_open"]]
    grid = [t for t in trades if t["bot"] == "Grid"]
    strat = [t for t in trades if t["bot"] == "Strategy"]

    grid_en = enrich_market(grid)
    wins_g = [t for t in grid_en if float(t["close_profit"] or 0) > 0]
    loss_g = [t for t in grid_en if float(t["close_profit"] or 0) <= 0]

    strat_by_tag = bucket_stats(strat, lambda t: (t.get("enter_tag") or "?").strip())

    patterns = {
        "grid": {
            "total": len(grid),
            "pnl": round(sum(float(t["close_profit_abs"] or 0) for t in grid), 4),
            "by_side": bucket_stats(grid, lambda t: "SHORT" if t.get("is_short") else "LONG"),
            "by_exit": bucket_stats(grid, lambda t: (t.get("exit_reason") or "?")[:30]),
            "by_hour_utc": bucket_stats(grid_en, lambda t: f"{t['hour_utc']:02d}h"),
            "by_ml_conf": bucket_stats(
                [t for t in grid if conf(t) is not None],
                lambda t: (
                    "<60%"
                    if (conf(t) or 0) < 0.6
                    else "60-69%"
                    if (conf(t) or 0) < 0.7
                    else "70-79%"
                    if (conf(t) or 0) < 0.8
                    else ">=80%"
                ),
            ),
            "repeat_pairs": _repeat_pair_stats(grid),
            "market_wins_vs_losses": {
                "wins_n": len(wins_g),
                "loss_n": len(loss_g),
                "adx_win": avg_market(wins_g, "adx_14"),
                "adx_loss": avg_market(loss_g, "adx_14"),
                "bb_width_win": avg_market(wins_g, "range_pct"),
                "bb_width_loss": avg_market(loss_g, "range_pct"),
                "rsi_win": avg_market(wins_g, "rsi_14"),
                "rsi_loss": avg_market(loss_g, "rsi_14"),
                "vol_ratio_win": avg_market(wins_g, "vol_ratio_20"),
                "vol_ratio_loss": avg_market(loss_g, "vol_ratio_20"),
            },
            "worst_pairs": sorted(
                ((p, round(v, 4)) for p, v in data["summary"]["Grid"]["by_pair"].items()),
                key=lambda x: x[1],
            )[:12],
            "best_pairs": sorted(
                ((p, round(v, 4)) for p, v in data["summary"]["Grid"]["by_pair"].items()),
                key=lambda x: x[1],
                reverse=True,
            )[:8],
        },
        "strategy": {
            "total": len(strat),
            "pnl": round(sum(float(t["close_profit_abs"] or 0) for t in strat), 4),
            "by_tag": strat_by_tag,
            "ml_vs_no_ml": {
                "with_ml": bucket_stats([t for t in strat if t.get("ml")], lambda _: "ml"),
                "no_ml": bucket_stats([t for t in strat if not t.get("ml")], lambda _: "no_ml"),
            },
            "ml_conf_buckets": bucket_stats(
                [t for t in strat if conf(t) is not None],
                lambda t: (
                    "<60%"
                    if (conf(t) or 0) < 0.6
                    else "60-69%"
                    if (conf(t) or 0) < 0.7
                    else ">=70%"
                ),
            ),
            "supertrend_hours": bucket_stats(
                [t for t in strat if "Supertrend" in (t.get("enter_tag") or "")],
                lambda t: f"{hour_utc(t):02d}h",
            ),
        },
    }

    # Sim backtest Jul 5 on prod grid pairs
    grid_pairs = sorted({t["pair"] for t in grid})
    sim = {}
    if grid_pairs:
        sim["live_strategy_jul5"] = run_grid_backtest(
            grid_pairs,
            "20250705-20250706",
            "VolatilityGridStrategy",
            "user_data/config_grid.json",
        )
        # subset: only pairs with losses > 0.3
        bad = [p for p, v in data["summary"]["Grid"]["by_pair"].items() if v < -0.3]
        if bad:
            sim["live_strategy_jul5_bad_pairs"] = run_grid_backtest(
                bad,
                "20250705-20250706",
                "VolatilityGridStrategy",
                "user_data/config_grid.json",
            )
        good = [p for p, v in data["summary"]["Grid"]["by_pair"].items() if v > 0.1]
        if good:
            sim["live_strategy_jul5_good_pairs"] = run_grid_backtest(
                good,
                "20250705-20250706",
                "VolatilityGridStrategy",
                "user_data/config_grid.json",
            )

    patterns["sim_backtest"] = sim
    return patterns


def _repeat_pair_stats(grid: list[dict]) -> dict[str, Any]:
    by_pair: dict[str, list[float]] = defaultdict(list)
    for t in grid:
        by_pair[t["pair"]].append(float(t["close_profit_abs"] or 0))
    repeat = {p: v for p, v in by_pair.items() if len(v) >= 2}
    repeat_loss = {p: sum(v) for p, v in repeat.items() if sum(v) < 0}
    return {
        "pairs_traded_2plus": len(repeat),
        "repeat_net_loss_pairs": len(repeat_loss),
        "top_repeat_losses": sorted(repeat_loss.items(), key=lambda x: x[1])[:8],
    }


def write_txt(p: dict) -> str:
    g = p["grid"]
    s = p["strategy"]
    lines = [
        "=" * 78,
        "  ЗАКОНОМЕРНОСТИ ПРОД · 5–6 ИЮЛЯ 2026 + ПРОВЕРКА НА СИМЕ",
        "=" * 78,
        "",
        "1. GRID (−6.20 USDT за 2 дня, 45 сделок 5 июля)",
        "-" * 78,
        f"  Всего: {g['total']} сд., PnL {g['pnl']:+.2f} USDT",
        "",
        "  1.1 Направление:",
    ]
    for k, v in g["by_side"].items():
        lines.append(f"    {k}: {v['n']} сд., {v['pnl']:+.2f} USDT, WR {v['wr']*100:.0f}%")

    lines += ["", "  1.2 Причина выхода:"]
    for k, v in sorted(g["by_exit"].items(), key=lambda x: x[1]["pnl"]):
        lines.append(f"    {k}: {v['n']}x, {v['pnl']:+.2f} USDT, WR {v['wr']*100:.0f}%")

    lines += ["", "  1.3 ML confidence (только сделки с меткой):"]
    for k, v in g["by_ml_conf"].items():
        lines.append(f"    {k}: {v['n']} сд., {v['pnl']:+.2f} USDT, WR {v['wr']*100:.0f}%")

    lines += ["", "  1.4 Час UTC (когда закрывались):"]
    for k, v in sorted(g["by_hour_utc"].items(), key=lambda x: x[1]["pnl"]):
        if v["n"] >= 3:
            lines.append(f"    {k}: {v['n']} сд., {v['pnl']:+.2f} USDT")

    m = g["market_wins_vs_losses"]
    lines += [
        "",
        "  1.5 Рынок на входе (5m фичи, sim datastore):",
        f"    ADX:  win avg {m['adx_win']} vs loss {m['adx_loss']}",
        f"    RSI:  win avg {m['rsi_win']} vs loss {m['rsi_loss']}",
        f"    range_pct: win {m['bb_width_win']} vs loss {m['bb_width_loss']}",
        f"    vol_ratio: win {m['vol_ratio_win']} vs loss {m['vol_ratio_loss']}",
        "",
        "  1.6 Повторные входы на одной паре:",
        f"    пар с 2+ сделками: {g['repeat_pairs']['pairs_traded_2plus']}",
        f"    из них в сумме в минусе: {g['repeat_pairs']['repeat_net_loss_pairs']}",
    ]
    for pair, pnl in g["repeat_pairs"]["top_repeat_losses"]:
        lines.append(f"      {pair}: {pnl:+.2f} USDT")

    lines += ["", "  1.7 Худшие / лучшие пары:"]
    for pair, pnl in g["worst_pairs"]:
        lines.append(f"    {pair}: {pnl:+.2f}")
    lines.append("    ---")
    for pair, pnl in g["best_pairs"]:
        lines.append(f"    {pair}: {pnl:+.2f}")

    lines += [
        "",
        "2. STRATEGY (−3.32 USDT)",
        "-" * 78,
        f"  Всего: {s['total']} сд., PnL {s['pnl']:+.2f} USDT",
        "",
        "  2.1 По стратегии (enter_tag):",
    ]
    for k, v in sorted(s["by_tag"].items(), key=lambda x: x[1]["pnl"]):
        lines.append(f"    {k}: {v['n']} сд., {v['pnl']:+.2f} USDT, WR {v['wr']*100:.0f}%")

    lines += ["", "  2.2 ML meta vs без ML:", "  2.3 ML confidence buckets:"]
    for k, v in s["ml_conf_buckets"].items():
        lines.append(f"    {k}: {v['n']} сд., {v['pnl']:+.2f} USDT, WR {v['wr']*100:.0f}%")

    lines += ["", "3. СИМУЛЯТОР — backtest 5 июля на тех же grid-парах", "-" * 78]
    for name, r in p.get("sim_backtest", {}).items():
        lines.append(f"  {name}: {r.get('trades')} сд., PnL {r.get('pnl_usdt')} USDT ({r.get('pairs')} пар)")

    lines += [
        "",
        "4. ВЫВОДЫ И РЕКОМЕНДАЦИИ",
        "-" * 78,
        _recommendations(p),
        "=" * 78,
    ]
    return "\n".join(lines) + "\n"


def _recommendations(p: dict) -> str:
    g = p["grid"]
    lines = []
    sl = g["by_exit"].get("stoploss_on_exchange", {})
    if sl.get("n", 0) > 20:
        lines.append("  • Grid: доминирует stoploss_on_exchange — входы в не-рейндже или слишком узкий SL (−3%).")
    low_ml = g["by_ml_conf"].get("<60%", {})
    if low_ml.get("n", 0) >= 5 and low_ml.get("pnl", 0) < 0:
        lines.append("  • Grid ML: сделки <60% conf — в минусе; поднять grid gate до 70% (уже в проде) или 75%.")
    rep = g["repeat_pairs"]
    if rep.get("repeat_net_loss_pairs", 0) >= 3:
        lines.append("  • Повторные входы на той же паре после SL — усилить cooldown / blacklist пар с 2+ SL за день.")
    m = g["market_wins_vs_losses"]
    if m.get("adx_loss") and m.get("adx_win") and m["adx_loss"] > m["adx_win"]:
        lines.append(f"  • Убыточные grid: ADX выше ({m['adx_loss']} vs {m['adx_win']}) — ужесточить adx_max в стратегии.")
    st = p["strategy"]["by_tag"].get("SupertrendStrategy", {})
    if st.get("pnl", 0) < 0 and st.get("n", 0) > 20:
        lines.append("  • Supertrend без ML (утро 5 июл): много мелких SL; не торговать без ML gate или отключить вне scanner.")
    sim = p.get("sim_backtest", {})
    live = sim.get("live_strategy_jul5", {})
    if live.get("trades", 0) > 0:
        lines.append(
            f"  • Sim backtest live VolatilityGrid на тех же парах за 5 июл: "
            f"{live.get('trades')} сд., {live.get('pnl_usdt')} USDT — сравни с продом."
        )
    if not lines:
        lines.append("  • См. таблицы выше.")
    return "\n".join(lines)


def main() -> int:
    print("Analyzing prod patterns...", flush=True)
    patterns = analyze()
    OUT_JSON.write_text(json.dumps(patterns, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    text = write_txt(patterns)
    OUT_TXT.write_text(text, encoding="utf-8")
    print(text)
    print(f"Saved: {OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
