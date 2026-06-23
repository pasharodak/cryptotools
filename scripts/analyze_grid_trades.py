#!/usr/bin/env python3
"""Analyze Grid trades with Bybit market context at entry/exit."""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"


def ft_pair_to_symbol(pair: str) -> str:
    # XRP/USDT:USDT -> XRPUSDT
    base = pair.split("/")[0]
    return f"{base}USDT"


def parse_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "+" not in s[10:] and s.endswith(" UTC"):
        s = s.replace(" UTC", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "5") -> list[list[str]]:
    rows: list[list[str]] = []
    cursor = start_ms
    while cursor < end_ms:
        q = urllib.parse.urlencode(
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": str(cursor),
                "end": str(end_ms),
                "limit": "200",
            }
        )
        req = urllib.request.Request(f"{BYBIT_KLINE}?{q}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        batch = data.get("result", {}).get("list", [])
        if not batch:
            break
        batch = list(reversed(batch))
        rows.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= cursor:
            break
        cursor = last_ts + 5 * 60 * 1000
        time.sleep(0.15)
    return rows


def calc_bb_adx(closes: list[float], highs: list[float], lows: list[float]) -> dict[str, float]:
    import numpy as np

    if len(closes) < 25:
        return {}
    c = np.array(closes, dtype=float)
    h = np.array(highs, dtype=float)
    l = np.array(lows, dtype=float)
    mid = c[-20:].mean()
    std = c[-20:].std()
    bb_u = mid + 2 * std
    bb_l = mid - 2 * std
    bb_width = (bb_u - bb_l) / mid if mid else 0

    # simple ADX approximation via directional movement
    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, min(15, len(c))):
        tr = max(h[-i] - l[-i], abs(h[-i] - c[-i - 1]), abs(l[-i] - c[-i - 1]))
        up = h[-i] - h[-i - 1]
        down = l[-i - 1] - l[-i]
        tr_list.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    atr = sum(tr_list) / len(tr_list) if tr_list else 1
    pdi = 100 * (sum(plus_dm) / len(plus_dm)) / atr if atr else 0
    mdi = 100 * (sum(minus_dm) / len(minus_dm)) / atr if atr else 0
    adx = abs(pdi - mdi)

    return {
        "bb_width_pct": round(bb_width * 100, 2),
        "adx_approx": round(adx, 1),
        "close": float(c[-1]),
        "bb_mid": round(mid, 6),
    }


def market_context(pair: str, open_dt: datetime, close_dt: datetime | None) -> dict[str, Any]:
    symbol = ft_pair_to_symbol(pair)
    start_ms = int((open_dt.timestamp() - 3600) * 1000)
    end_ms = int(((close_dt or open_dt).timestamp() + 3600) * 1000)
    try:
        rows = fetch_klines(symbol, start_ms, end_ms)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "symbol": symbol}

    if not rows:
        return {"error": "no klines", "symbol": symbol}

    def slice_at(ts_ms: int) -> dict[str, Any]:
        subset = [r for r in rows if int(r[0]) <= ts_ms]
        if len(subset) < 20:
            subset = rows[: max(20, len(subset))]
        closes = [float(r[4]) for r in subset]
        highs = [float(r[2]) for r in subset]
        lows = [float(r[3]) for r in subset]
        m = calc_bb_adx(closes, highs, lows)
        # price move next 12 candles (1h on 5m)
        idx = len(subset) - 1
        future = rows[idx : idx + 13] if idx < len(rows) else []
        if future:
            p0 = float(subset[-1][4])
            p1 = float(future[-1][4])
            m["move_1h_pct"] = round((p1 - p0) / p0 * 100, 2)
        return m

    open_ms = int(open_dt.timestamp() * 1000)
    ctx = {"symbol": symbol, "at_entry": slice_at(open_ms)}
    if close_dt:
        close_ms = int(close_dt.timestamp() * 1000)
        ctx["at_exit"] = slice_at(close_ms)
        # trend during trade
        open_p = float(ctx["at_entry"].get("close", 0))
        close_p = float(ctx["at_exit"].get("close", 0))
        if open_p:
            ctx["trade_price_move_pct"] = round((close_p - open_p) / open_p * 100, 2)
    return ctx


def classify_trade(t: dict[str, Any]) -> str:
    reason = t.get("exit_reason") or "unknown"
    profit = float(t.get("profit_abs") or 0)
    is_short = bool(t.get("is_short"))
    move = t.get("market", {}).get("trade_price_move_pct")
    adx_exit = t.get("market", {}).get("at_exit", {}).get("adx_approx")
    entries = t.get("entry_count", 1)

    if profit >= 0:
        if reason == "partial_tp_bb_mid" or "roi" in reason:
            return "win_take_profit"
        return "win_other"

    if reason == "stop_loss":
        if entries >= 3:
            return "loss_stop_after_dca"
        return "loss_stop_quick"

    if reason == "exit_signal":
        if adx_exit and adx_exit > 30:
            return "loss_trend_break"
        if move is not None:
            if (not is_short and move < -1.5) or (is_short and move > 1.5):
                return "loss_against_trend"
        return "loss_exit_signal"

    if reason == "force_exit":
        return "loss_force"
    return "loss_other"


def analyze(export_path: Path, report_path: Path) -> dict[str, Any]:
    data = json.loads(export_path.read_text(encoding="utf-8"))
    trades = [t for t in data["trades"] if t.get("is_open") == 0]

    enriched = []
    for i, t in enumerate(trades):
        open_dt = parse_dt(str(t["open_date"]))
        close_dt = parse_dt(str(t["close_date"])) if t.get("close_date") else None
        print(f"[{i+1}/{len(trades)}] {t['pair']} {t.get('exit_reason')} ...")
        mkt = market_context(t["pair"], open_dt, close_dt)
        row = {
            "id": t["id"],
            "pair": t["pair"],
            "is_short": bool(t.get("is_short")),
            "open_date": t["open_date"],
            "close_date": t["close_date"],
            "open_rate": t.get("open_rate"),
            "close_rate": t.get("close_rate"),
            "stake_amount": t.get("stake_amount"),
            "max_stake_amount": t.get("max_stake_amount"),
            "amount": t.get("amount"),
            "leverage": t.get("leverage"),
            "profit_abs": t.get("close_profit_abs"),
            "profit_ratio": t.get("close_profit"),
            "exit_reason": t.get("exit_reason"),
            "entry_count": t.get("entry_count", 1),
            "orders": [
                {
                    "side": "entry" if o.get("ft_is_entry") else "exit",
                    "tag": o.get("ft_order_tag"),
                    "stake": o.get("stake_amount"),
                    "amount": o.get("amount"),
                    "price": o.get("average"),
                    "date": o.get("order_filled_date") or o.get("order_date"),
                }
                for o in t.get("orders", [])
                if o.get("status") == "closed"
            ],
            "market": mkt,
        }
        row["loss_class"] = classify_trade(row)
        enriched.append(row)
        time.sleep(0.2)

    wins = [t for t in enriched if float(t.get("profit_abs") or 0) >= 0]
    losses = [t for t in enriched if float(t.get("profit_abs") or 0) < 0]

    by_pair = defaultdict(lambda: {"n": 0, "profit": 0.0, "wins": 0, "losses": 0})
    by_reason = defaultdict(lambda: {"n": 0, "profit": 0.0})
    by_class = defaultdict(lambda: {"n": 0, "profit": 0.0})

    for t in enriched:
        p = t["pair"]
        pr = float(t.get("profit_abs") or 0)
        by_pair[p]["n"] += 1
        by_pair[p]["profit"] += pr
        if pr >= 0:
            by_pair[p]["wins"] += 1
        else:
            by_pair[p]["losses"] += 1
        by_reason[t.get("exit_reason") or "?"]["n"] += 1
        by_reason[t.get("exit_reason") or "?"]["profit"] += pr
        by_class[t["loss_class"]]["n"] += 1
        by_class[t["loss_class"]]["profit"] += pr

    worst = sorted(losses, key=lambda x: float(x.get("profit_abs") or 0))[:10]
    best = sorted(wins, key=lambda x: float(x.get("profit_abs") or 0), reverse=True)[:5]

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total_closed": len(enriched),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100 * len(wins) / len(enriched), 1) if enriched else 0,
            "total_profit_usdt": round(sum(float(t.get("profit_abs") or 0) for t in enriched), 4),
            "avg_win_usdt": round(sum(float(t.get("profit_abs") or 0) for t in wins) / len(wins), 4)
            if wins
            else 0,
            "avg_loss_usdt": round(sum(float(t.get("profit_abs") or 0) for t in losses) / len(losses), 4)
            if losses
            else 0,
            "avg_entries_loss": round(
                sum(t.get("entry_count", 1) for t in losses) / len(losses), 2
            )
            if losses
            else 0,
            "avg_entries_win": round(sum(t.get("entry_count", 1) for t in wins) / len(wins), 2)
            if wins
            else 0,
        },
        "by_pair": dict(sorted(by_pair.items(), key=lambda x: x[1]["profit"])),
        "by_exit_reason": dict(by_reason),
        "by_loss_class": dict(by_class),
        "worst_trades": worst,
        "best_trades": best,
        "all_trades": enriched,
    }

    # Markdown narrative
    md_lines = [
        "# Анализ сделок Grid-бота (VolatilityGridStrategy)",
        "",
        f"Дата отчёта: {report['generated_at']}",
        f"Закрытых сделок: {report['summary']['total_closed']}",
        f"Win rate: {report['summary']['win_rate_pct']}%",
        f"Суммарный PnL: **{report['summary']['total_profit_usdt']} USDT**",
        "",
        "## Сводка",
        "",
        f"- Средний выигрыш: {report['summary']['avg_win_usdt']} USDT",
        f"- Средний проигрыш: {report['summary']['avg_loss_usdt']} USDT",
        f"- Среднее число входов в минусах: {report['summary']['avg_entries_loss']}",
        f"- Среднее число входов в плюсах: {report['summary']['avg_entries_win']}",
        "",
        "## По парам",
        "",
        "| Пара | Сделок | W/L | PnL USDT |",
        "|------|--------|-----|----------|",
    ]
    for pair, st in sorted(by_pair.items(), key=lambda x: x[1]["profit"]):
        md_lines.append(
            f"| {pair} | {st['n']} | {st['wins']}/{st['losses']} | {st['profit']:.4f} |"
        )

    md_lines += [
        "",
        "## Причины выхода",
        "",
        "| exit_reason | Сделок | PnL |",
        "|-------------|--------|-----|",
    ]
    for reason, st in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
        md_lines.append(f"| {reason} | {st['n']} | {st['profit']:.4f} |")

    md_lines += [
        "",
        "## Классификация проигрышей",
        "",
    ]
    for cls, st in sorted(by_class.items(), key=lambda x: x[1]["profit"]):
        md_lines.append(f"- **{cls}**: {st['n']} сделок, {st['profit']:.4f} USDT")

    md_lines += [
        "",
        "## Худшие сделки",
        "",
    ]
    for t in worst:
        m = t.get("market", {})
        md_lines.append(
            f"### {t['pair']} id={t['id']} ({t['profit_abs']} USDT, {t['exit_reason']})"
        )
        md_lines.append(
            f"- {'SHORT' if t['is_short'] else 'LONG'}, входов: {t['entry_count']}, "
            f"stake max: {t.get('max_stake_amount')}"
        )
        ae = m.get("at_entry", {})
        ax = m.get("at_exit", {})
        md_lines.append(
            f"- На входе: BB width {ae.get('bb_width_pct')}%, ADX~{ae.get('adx_approx')}"
        )
        if ax:
            md_lines.append(
                f"- На выходе: BB width {ax.get('bb_width_pct')}%, ADX~{ax.get('adx_approx')}, "
                f"движение цены за сделку: {m.get('trade_price_move_pct')}%"
            )
        md_lines.append(f"- Класс: {t['loss_class']}")
        md_lines.append("")

    md_lines += [
        "",
        "## Выводы и рекомендации",
        "",
        "### Почему убытки 0.5–1.2 USDT при stake 5 и SL 5%",
        "- Stake 5 USDT — **на каждый вход**; Grid делает до 4 входов (DCA) → до ~20 USDT маржи.",
        "- Стоп −5% считается от **суммарной** маржи: 20×5% ≈ 1.0 USDT.",
        "",
        "### Типичные проблемы mean-reversion / grid на фьючах",
        "1. **Ложный боковик** — ADX<28 и широкие BB не гарантируют отсутствие тренда; монета может пробить полосу и уйти.",
        "2. **DCA против тренда** — усреднение увеличивает exposure именно когда рынок идёт против.",
        "3. **Выход по trend_break (ADX>35)** часто с большим минусом, если тренд развернулся резко.",
        "4. **Высокобета альты** из сканера волатильнее majors — grid на них опаснее.",
        "5. **3x плечо** ускоряет достижение стопа по цене.",
        "",
        "### Что улучшить",
        "- Снизить `max_entry_position_adjustment` до 1–2 или stake до 2–3 USDT.",
        "- Ужесточить сканер: выше `min_active_ratio`, ниже ADX, уже BB (настоящий range).",
        "- Исключить пары с повторяющимися убытками из whitelist.",
        "- Рассмотреть выход по стопу раньше trend_break; не усреднять если ADX растёт.",
        "- Для grid классика: торговать majors (BTC/ETH/SOL) с меньшим плечом (1–2x).",
        "",
    ]

    report_path.parent.mkdir(parents=True, exist_ok=True)
    json_out = report_path.with_suffix(".json")
    md_out = report_path.with_suffix(".md")
    json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md_out.write_text("\n".join(md_lines), encoding="utf-8")
    print(json.dumps({"json": str(json_out), "md": str(md_out), "trades": len(enriched)}))
    return report


if __name__ == "__main__":
    import sys

    export = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("grid_trades_export.json")
    report = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("grid_trades_analysis")
    analyze(export, report)
