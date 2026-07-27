#!/usr/bin/env python3
"""Re-aggregate grid analysis from cached JSON using correct PnL fields."""
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

export = json.loads(Path("D:/cryptotools/site/user_data/grid_trades_export.json").read_text(encoding="utf-8"))
cached = json.loads(Path("D:/cryptotools/site/user_data/grid_trades_analysis.json").read_text(encoding="utf-8"))
by_id = {t["id"]: t for t in cached["all_trades"]}

enriched = []
for t in export["trades"]:
    if t.get("is_open") != 0:
        continue
    row = by_id.get(t["id"], {})
    pr = float(t.get("close_profit_abs") or 0)
    enriched.append({
        **row,
        "profit_abs": pr,
        "profit_ratio": t.get("close_profit"),
        "max_stake_amount": t.get("max_stake_amount"),
        "exit_reason": t.get("exit_reason"),
        "entry_count": t.get("entry_count", row.get("entry_count", 1)),
        "pair": t["pair"],
        "is_short": bool(t.get("is_short")),
        "open_date": t["open_date"],
        "close_date": t["close_date"],
    })

wins = [x for x in enriched if x["profit_abs"] >= 0]
losses = [x for x in enriched if x["profit_abs"] < 0]

by_pair = defaultdict(lambda: {"n": 0, "profit": 0.0, "wins": 0, "losses": 0, "sl": 0})
by_reason = defaultdict(lambda: {"n": 0, "profit": 0.0})
for t in enriched:
    p = t["pair"]
    pr = t["profit_abs"]
    by_pair[p]["n"] += 1
    by_pair[p]["profit"] += pr
    if pr >= 0:
        by_pair[p]["wins"] += 1
    else:
        by_pair[p]["losses"] += 1
        if "stoploss" in (t.get("exit_reason") or ""):
            by_pair[p]["sl"] += 1
    by_reason[t.get("exit_reason") or "?"]["n"] += 1
    by_reason[t.get("exit_reason") or "?"]["profit"] += pr

# loss size buckets
buckets = {"0.25": 0, "0.5": 0, "0.75": 0, "1.0+": 0}
for t in losses:
    a = abs(t["profit_abs"])
    if a < 0.35:
        buckets["0.25"] += 1
    elif a < 0.6:
        buckets["0.5"] += 1
    elif a < 0.9:
        buckets["0.75"] += 1
    else:
        buckets["1.0+"] += 1

worst = sorted(losses, key=lambda x: x["profit_abs"])[:15]
best = sorted(wins, key=lambda x: x["profit_abs"], reverse=True)[:5]

report = {
    "generated_at": datetime.now(UTC).isoformat(),
    "summary": {
        "total_closed": len(enriched),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(enriched), 1),
        "total_profit_usdt": round(sum(t["profit_abs"] for t in enriched), 4),
        "avg_win_usdt": round(sum(t["profit_abs"] for t in wins) / len(wins), 4) if wins else 0,
        "avg_loss_usdt": round(sum(t["profit_abs"] for t in losses) / len(losses), 4) if losses else 0,
        "avg_entries_loss": round(sum(t.get("entry_count", 1) for t in losses) / len(losses), 2) if losses else 0,
        "avg_max_stake_loss": round(sum(float(t.get("max_stake_amount") or 0) for t in losses) / len(losses), 2) if losses else 0,
        "loss_size_buckets": buckets,
    },
    "by_pair": dict(sorted(by_pair.items(), key=lambda x: x[1]["profit"])),
    "by_exit_reason": dict(by_reason),
    "worst_trades": worst,
    "best_trades": best,
}

lines = [
    "# Анализ сделок Grid-бота",
    "",
    f"**{report['summary']['total_closed']}** закрытых сделок | Win rate **{report['summary']['win_rate_pct']}%** | PnL **{report['summary']['total_profit_usdt']} USDT**",
    "",
    "## Сводка",
    f"- Выигрышей: {report['summary']['wins']}, средний +{report['summary']['avg_win_usdt']} USDT",
    f"- Проигрышей: {report['summary']['losses']}, средний {report['summary']['avg_loss_usdt']} USDT",
    f"- Средний max_stake в минусах: {report['summary']['avg_max_stake_loss']} USDT",
    f"- Среднее число DCA-входов в минусах: {report['summary']['avg_entries_loss']}",
    f"- Размер убытков: ~0.25: {buckets['0.25']}, ~0.5: {buckets['0.5']}, ~0.75: {buckets['0.75']}, 1.0+: {buckets['1.0+']}",
    "",
    "## Худшие пары (PnL)",
    "| Пара | Сделок | W/L | Стопов | PnL |",
    "|------|--------|-----|--------|-----|",
]
for pair, st in sorted(by_pair.items(), key=lambda x: x[1]["profit"])[:15]:
    lines.append(f"| {pair} | {st['n']} | {st['wins']}/{st['losses']} | {st['sl']} | {st['profit']:.3f} |")

lines += ["", "## Причины выхода", "| reason | n | PnL |", "|--------|---|-----|"]
for r, st in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
    lines.append(f"| {r} | {st['n']} | {st['profit']:.3f} |")

lines += ["", "## 15 худших сделок"]
for t in worst:
    m = t.get("market", {})
    ae, ax = m.get("at_entry", {}), m.get("at_exit", {})
    side = "SHORT" if t.get("is_short") else "LONG"
    lines.append(f"\n### {t['pair']} {side} — **{t['profit_abs']:.3f} USDT** ({t.get('exit_reason')})")
    lines.append(f"- Входов: {t.get('entry_count')}, max stake: {t.get('max_stake_amount')}")
    lines.append(f"- {t.get('open_date')} → {t.get('close_date')}")
    if ae:
        lines.append(f"- На входе: BB {ae.get('bb_width_pct')}%, ADX~{ae.get('adx_approx')}")
    if ax:
        lines.append(f"- На выходе: BB {ax.get('bb_width_pct')}%, ADX~{ax.get('adx_approx')}, ход цены: {m.get('trade_price_move_pct')}%")

Path("D:/cryptotools/site/user_data/grid_trades_analysis.json").write_text(
    json.dumps({**cached, **report, "all_trades": enriched}, indent=2, ensure_ascii=False, default=str),
    encoding="utf-8",
)
Path("D:/cryptotools/site/user_data/grid_trades_analysis.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps(report["summary"], indent=2))
