#!/usr/bin/env python3
"""Period trade report + Cursor agent analysis for the Analytics «Отчёт» tab."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

APP_TZ = timezone(timedelta(hours=3))
REPORT_TIMEOUT_SEC = 240.0
MAX_TRADES_IN_PROMPT = 180
MAX_TRADES_IN_RESPONSE = 2000

_PERIOD_LABELS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "7d": "Неделя",
    "week": "Неделя",
    "custom": "Интервал",
}


def _parse_close_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            return int(val)
        if val > 1e9:
            return int(val * 1000)
        return None
    text = str(raw).strip()
    if not text:
        return None
    # epoch millis / seconds
    if re.fullmatch(r"\d{10,13}", text):
        n = int(text)
        return n if n > 1e12 else n * 1000
    # ISO-ish / sqlite: 2026-10-28 12:00:00(+00:00)?
    cleaned = text.replace("Z", "+00:00")
    cleaned = re.sub(r"\s+UTC\+3\b", "+03:00", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+UTC\b", "+00:00", cleaned, flags=re.I)
    cleaned = cleaned.replace(" ", "T", 1) if "T" not in cleaned[:11] else cleaned
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            # ctengine stores UTC naive usually
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _start_of_day_ms(ms: int | None = None) -> int:
    now = datetime.now(tz=APP_TZ) if ms is None else datetime.fromtimestamp(ms / 1000.0, tz=APP_TZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp() * 1000)


def _end_of_day_ms(ms: int | None = None) -> int:
    return _start_of_day_ms(ms) + 86_400_000 - 1


def _parse_date_input(value: str, *, end: bool = False) -> int | None:
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    dt = datetime(y, mo, d, tzinfo=APP_TZ)
    if end:
        dt = dt + timedelta(days=1) - timedelta(milliseconds=1)
    return int(dt.timestamp() * 1000)


def resolve_period(
    period: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return from_ms / to_ms / label for a report window (APP_TZ = UTC+3)."""
    raw = str(period or "").strip().lower()
    if raw in {"week"}:
        raw = "7d"
    df = str(date_from or "").strip()
    dt = str(date_to or "").strip()
    if df or dt or raw == "custom":
        from_ms = _parse_date_input(df, end=False) if df else 0
        to_ms = _parse_date_input(dt, end=True) if dt else int(time.time() * 1000)
        if from_ms is None:
            raise ValueError("некорректная дата «с»")
        if dt and to_ms is None:
            raise ValueError("некорректная дата «по»")
        if from_ms > to_ms:
            raise ValueError("дата «с» позже даты «по»")
        label = f"{df or '…'} — {dt or '…'}"
        return {
            "period": "custom",
            "label": label,
            "date_from": df or None,
            "date_to": dt or None,
            "from_ms": int(from_ms),
            "to_ms": int(to_ms),
            "tz": "UTC+3",
        }

    now_ms = int(time.time() * 1000)
    start_today = _start_of_day_ms(now_ms)
    if raw == "today":
        from_ms, to_ms = start_today, _end_of_day_ms(now_ms)
    elif raw == "yesterday":
        from_ms, to_ms = start_today - 86_400_000, start_today - 1
    elif raw == "7d":
        from_ms, to_ms = now_ms - 7 * 86_400_000, now_ms
    else:
        raise ValueError("period: today | yesterday | 7d | custom")

    return {
        "period": raw,
        "label": _PERIOD_LABELS.get(raw, raw),
        "date_from": datetime.fromtimestamp(from_ms / 1000.0, tz=APP_TZ).strftime("%Y-%m-%d"),
        "date_to": datetime.fromtimestamp(to_ms / 1000.0, tz=APP_TZ).strftime("%Y-%m-%d"),
        "from_ms": from_ms,
        "to_ms": to_ms,
        "tz": "UTC+3",
    }


def _compact_trade(bot: str, trade: dict[str, Any]) -> dict[str, Any] | None:
    close_ms = _parse_close_ms(trade.get("close_date") or trade.get("close_fill_date"))
    if close_ms is None:
        return None
    pnl_abs = trade.get("close_profit_abs")
    if pnl_abs is None:
        pnl_abs = trade.get("profit_abs")
    if pnl_abs is None:
        pnl_abs = trade.get("realized_profit")
    try:
        pnl_abs_f = float(pnl_abs) if pnl_abs is not None else 0.0
    except (TypeError, ValueError):
        pnl_abs_f = 0.0
    pnl_pct = trade.get("close_profit_pct")
    if pnl_pct is None and trade.get("close_profit") is not None:
        try:
            pnl_pct = round(float(trade["close_profit"]) * 100, 2)
        except (TypeError, ValueError):
            pnl_pct = None
    tag = str(trade.get("enter_tag") or trade.get("strategy") or "").strip()
    return {
        "bot": bot,
        "id": trade.get("trade_id") or trade.get("id"),
        "pair": trade.get("pair") or "",
        "side": "short" if trade.get("is_short") else "long",
        "strategy": tag or str(trade.get("strategy") or ""),
        "open": trade.get("open_date"),
        "close": trade.get("close_date") or trade.get("close_fill_date"),
        "close_ms": close_ms,
        "pnl_abs": round(pnl_abs_f, 4),
        "pnl_pct": pnl_pct,
        "exit": trade.get("exit_reason") or "",
        "stake": trade.get("stake_amount"),
    }


def _summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = losses = flats = 0
    pnl = 0.0
    by_strategy: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )
    by_pair: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )
    by_side = {"long": {"n": 0, "pnl": 0.0}, "short": {"n": 0, "pnl": 0.0}}
    for t in trades:
        p = float(t.get("pnl_abs") or 0.0)
        pnl += p
        if p > 0:
            wins += 1
        elif p < 0:
            losses += 1
        else:
            flats += 1
        strat = str(t.get("strategy") or "—")[:80]
        pair = str(t.get("pair") or "—")
        side = "short" if t.get("side") == "short" else "long"
        for bucket, key in ((by_strategy, strat), (by_pair, pair)):
            row = bucket[key]
            row["n"] = int(row["n"]) + 1
            row["pnl"] = float(row["pnl"]) + p
            if p > 0:
                row["wins"] = int(row["wins"]) + 1
            elif p < 0:
                row["losses"] = int(row["losses"]) + 1
        by_side[side]["n"] = int(by_side[side]["n"]) + 1
        by_side[side]["pnl"] = float(by_side[side]["pnl"]) + p

    def _top(src: dict[str, dict[str, float | int]], limit: int = 12) -> list[dict[str, Any]]:
        rows = [
            {
                "name": name,
                "n": int(v["n"]),
                "wins": int(v["wins"]),
                "losses": int(v["losses"]),
                "pnl": round(float(v["pnl"]), 4),
            }
            for name, v in src.items()
        ]
        rows.sort(key=lambda r: abs(float(r["pnl"])), reverse=True)
        return rows[:limit]

    closed = wins + losses + flats
    return {
        "trades": closed,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "winrate_pct": round(100.0 * wins / (wins + losses), 1) if (wins + losses) else None,
        "pnl_abs": round(pnl, 4),
        "by_strategy": _top(by_strategy),
        "by_pair": _top(by_pair),
        "by_side": {
            "long": {"n": int(by_side["long"]["n"]), "pnl": round(float(by_side["long"]["pnl"]), 4)},
            "short": {"n": int(by_side["short"]["n"]), "pnl": round(float(by_side["short"]["pnl"]), 4)},
        },
    }


def collect_period_trades(
    closed_by_bot: dict[str, list[dict[str, Any]]],
    *,
    from_ms: int,
    to_ms: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bot, trades in closed_by_bot.items():
        if bot.startswith("_") or not isinstance(trades, list):
            continue
        for trade in trades:
            if trade.get("is_open"):
                continue
            item = _compact_trade(str(bot), trade)
            if not item:
                continue
            cms = int(item["close_ms"])
            if cms < from_ms or cms > to_ms:
                continue
            out.append(item)
    out.sort(key=lambda t: int(t.get("close_ms") or 0), reverse=True)
    return out


def build_report_payload(
    closed_by_bot: dict[str, list[dict[str, Any]]],
    *,
    period: str | None = "today",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    window = resolve_period(period, date_from, date_to)
    trades = collect_period_trades(
        closed_by_bot, from_ms=int(window["from_ms"]), to_ms=int(window["to_ms"])
    )
    truncated = False
    trades_out = trades
    if len(trades_out) > MAX_TRADES_IN_RESPONSE:
        trades_out = trades_out[:MAX_TRADES_IN_RESPONSE]
        truncated = True
    summary = _summarize(trades)
    # strip close_ms from public trades list (keep in prompt build)
    public_trades = [{k: v for k, v in t.items() if k != "close_ms"} for t in trades_out]
    return {
        "period": {
            "id": window["period"],
            "label": window["label"],
            "date_from": window.get("date_from"),
            "date_to": window.get("date_to"),
            "tz": window["tz"],
        },
        "summary": summary,
        "trades": public_trades,
        "trades_total": len(trades),
        "truncated": truncated,
        "_window_ms": {"from": window["from_ms"], "to": window["to_ms"]},
    }


_DEFAULT_AGENT_INSTRUCTIONS = (
    "Ты аналитик крипто-торговли Bybit USDT Perpetual для панели CryptoTools.\n"
    "Тебе дан JSON закрытых сделок за выбранный период (время UTC+3).\n"
    "Напиши краткий отчёт на русском языке обычным текстом (можно markdown).\n"
    "Структура строго такая:\n"
    "## Итог\n"
    "## Что было\n"
    "## Стоит делать\n"
    "## Не стоит делать\n"
    "Будь конкретным: стратегии, пары, стороны, winrate, PnL. "
    "Не выдумывай сделки, которых нет в JSON. Не предлагай взлом или манипуляции рынком."
)

MAX_CUSTOM_PROMPT_CHARS = 8000


def _agent_prompt(payload: dict[str, Any], *, custom_prompt: str | None = None) -> str:
    trades = payload.get("trades") or []
    prompt_trades = trades[:MAX_TRADES_IN_PROMPT]
    note = ""
    if len(trades) > MAX_TRADES_IN_PROMPT:
        note = (
            f"\nВ JSON ниже только первые {MAX_TRADES_IN_PROMPT} сделок из "
            f"{payload.get('trades_total')}; опирайся также на summary.\n"
        )
    body = {
        "period": payload.get("period"),
        "summary": payload.get("summary"),
        "trades": prompt_trades,
    }
    instructions = str(custom_prompt or "").strip()
    if not instructions:
        instructions = _DEFAULT_AGENT_INSTRUCTIONS
    else:
        instructions = (
            f"{instructions}\n\n"
            "Тебе дан JSON закрытых сделок за выбранный период (время UTC+3). "
            "Не выдумывай сделки, которых нет в JSON."
        )
    return (
        f"{instructions}\n"
        f"{note}\n"
        "JSON:\n"
        f"{json.dumps(body, ensure_ascii=False)}"
    )


def analyze_with_cursor(
    payload: dict[str, Any],
    *,
    custom_prompt: str | None = None,
    timeout: float = REPORT_TIMEOUT_SEC,
) -> dict[str, Any]:
    import cursor_client

    if not cursor_client.configured():
        raise RuntimeError("CURSOR token is not set")
    prompt_text = str(custom_prompt or "").strip()
    if len(prompt_text) > MAX_CUSTOM_PROMPT_CHARS:
        raise ValueError(f"промпт слишком длинный (макс. {MAX_CUSTOM_PROMPT_CHARS} символов)")
    t0 = time.monotonic()
    text = cursor_client.complete(
        _agent_prompt(payload, custom_prompt=prompt_text or None),
        timeout=timeout,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return {
        "analysis": text.strip(),
        "model": cursor_client.model_name(),
        "elapsed_ms": elapsed_ms,
        "custom_prompt": bool(prompt_text),
    }
