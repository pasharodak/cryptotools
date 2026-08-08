#!/usr/bin/env python3
"""System changelog — ML Finder, strategies, Grid, scanners, Bybit Grid, UI."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bybit_grid_manager import ft_base, load_json, save_json

CHANGELOG_FILE = "user_data/grid_changelog.json"
SEED_VERSION = 7

CATEGORY_LABELS = {
    "telegram_bot": "Telegram-бот (Kronos)",
    "finder": "ML Finder",
    "strategy": "Стратегии",
    "grid_ft": "Grid",
    "ranging_scanner": "Сканер боковика",
    "strategy_scanner": "Сканер пар стратегий",
    "bybit_grid": "Bybit Grid",
    "deploy_guards": "Фильтры деплоя",
    "ui_infra": "UI и сервер",
    "system": "Система",
}

FIELD_LABELS: dict[str, str] = {
    "stop_loss_usdt": "Стоп-лосс",
    "take_profit_usdt": "Тейк-профит",
    "total_investment": "Инвестиция на бота",
    "max_active_bots": "Макс. активных Bybit Grid",
    "max_open_trades": "Макс. открытых сделок",
    "ml_gate_min_confidence": "Порог ML (profit)",
    "cell_number": "Количество сеток",
    "price_range_pct": "Ширина диапазона",
    "leverage": "Плечо",
    "neutral_max_1h_move_pct": "Лимит 1ч для Neutral",
    "sl_cooldown_hours": "Cooldown после SL",
    "sl_adaptive_min_count": "Порог SL для адаптивного TP",
    "take_profit_usdt_frequent_sl": "TP при частых SL",
    "adx_max": "ADX макс. (сканер)",
    "min_ranging_ratio": "Ranging ratio мин.",
    "max_ema50_slope": "Наклон EMA50 макс.",
}

FIELD_UNITS: dict[str, str] = {
    "stop_loss_usdt": "USDT",
    "take_profit_usdt": "USDT",
    "total_investment": "USDT",
    "price_range_pct": "%",
    "neutral_max_1h_move_pct": "%",
    "sl_cooldown_hours": "ч",
    "leverage": "x",
    "max_ema50_slope": "",
}


def changelog_path():
    return ft_base() / CHANGELOG_FILE


def load_changelog() -> dict[str, Any]:
    data = load_json(changelog_path(), {"seeded_version": 0, "entries": []})
    data.setdefault("entries", [])
    return data


def save_changelog(data: dict[str, Any]) -> None:
    save_json(changelog_path(), data)


def _fmt_value(field: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if field == "price_range_pct":
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(value)
    if field == "leverage":
        return f"{value}x"
    if field == "max_ema50_slope":
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)
    unit = FIELD_UNITS.get(field, "")
    if unit:
        return f"{value} {unit}".strip()
    return str(value)


def format_change_text(field: str, old: Any, new: Any, *, label: str | None = None) -> str:
    title = label or FIELD_LABELS.get(field, field)
    if old is None or old == "":
        return f"{title} установлен: {_fmt_value(field, new)}"
    return f"{title} изменён: {_fmt_value(field, old)} → {_fmt_value(field, new)}"


def _insert_entry(entry: dict[str, Any]) -> dict[str, Any]:
    data = load_changelog()
    data["entries"].insert(0, entry)
    data["entries"] = data["entries"][:250]
    save_changelog(data)
    return entry


def append_entry(
    *,
    field: str,
    old: Any,
    new: Any,
    category: str = "bybit_grid",
    source: str = "auto",
    note: str | None = None,
    at: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    if old is not None and str(old) == str(new):
        return {}
    entry = {
        "at": at or datetime.now(UTC).isoformat(),
        "category": category,
        "field": field,
        "old": old,
        "new": new,
        "text": format_change_text(field, old, new, label=label),
        "source": source,
    }
    if note:
        entry["note"] = note
    return _insert_entry(entry)


def append_note(
    *,
    text: str,
    category: str = "system",
    at: str | None = None,
    source: str = "manual",
    note: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "at": at or datetime.now(UTC).isoformat(),
        "category": category,
        "field": "_note",
        "text": text,
        "source": source,
    }
    if note:
        entry["note"] = note
    return _insert_entry(entry)


def record_config_diff(old_cfg: dict[str, Any], new_cfg: dict[str, Any], *, source: str = "ui") -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    old_def = old_cfg.get("defaults") or {}
    new_def = new_cfg.get("defaults") or {}
    for key in (
        "stop_loss_usdt",
        "take_profit_usdt",
        "total_investment",
        "cell_number",
        "price_range_pct",
        "leverage",
    ):
        if key in new_def and str(old_def.get(key)) != str(new_def.get(key)):
            e = append_entry(field=key, old=old_def.get(key), new=new_def.get(key), source=source)
            if e:
                recorded.append(e)
    if str(old_cfg.get("max_active_bots")) != str(new_cfg.get("max_active_bots")):
        e = append_entry(
            field="max_active_bots",
            old=old_cfg.get("max_active_bots"),
            new=new_cfg.get("max_active_bots"),
            source=source,
        )
        if e:
            recorded.append(e)
    old_g = old_cfg.get("deploy_guards") or {}
    new_g = new_cfg.get("deploy_guards") or {}
    for key in (
        "neutral_max_1h_move_pct",
        "sl_cooldown_hours",
        "sl_adaptive_min_count",
        "take_profit_usdt_frequent_sl",
    ):
        if key in new_g and str(old_g.get(key)) != str(new_g.get(key)):
            e = append_entry(
                field=key,
                old=old_g.get(key),
                new=new_g.get(key),
                category="deploy_guards",
                source=source,
            )
            if e:
                recorded.append(e)
    return recorded


def record_max_open_trades(bot: str, old: int, new: int) -> None:
    bot_labels = {"finder": "ML Finder", "strategy": "Стратегии", "grid": "Grid"}
    label = f"Макс. сделок ({bot_labels.get(bot, bot)})"
    append_entry(
        field="max_open_trades",
        old=old,
        new=new,
        category={"finder": "finder", "strategy": "strategy", "grid": "grid_ft"}.get(bot, "system"),
        label=label,
    )


def record_stake_amount(bot: str, old: float, new: float) -> None:
    bot_labels = {"grid": "Grid", "strategy": "Стратегии"}
    label = f"Stake USDT ({bot_labels.get(bot, bot)})"
    append_entry(
        field="stake_amount",
        old=old,
        new=new,
        category={"grid": "grid_ft", "strategy": "strategy"}.get(bot, "system"),
        label=label,
    )


def record_strategy_risk(old: dict[str, float], new: dict[str, float]) -> None:
    old_sl = abs(float(old.get("stoploss", 0))) * 100
    new_sl = abs(float(new.get("stoploss", 0))) * 100
    old_tp = float(old.get("take_profit", 0)) * 100
    new_tp = float(new.get("take_profit", 0)) * 100
    append_entry(
        field="strategy_risk",
        old=f"SL −{old_sl:g}% · TP +{old_tp:g}%",
        new=f"SL −{new_sl:g}% · TP +{new_tp:g}%",
        category="strategy",
        label="Стоп / тейк стратегий",
    )


def record_ml_gate_confidence(old: float, new: float, *, source: str = "deploy") -> None:
    """Log ML gate profit threshold change for all prod bots (changelog UI)."""
    if old is not None and abs(float(old) - float(new)) < 1e-9:
        return
    old_pct = f"{int(round(float(old) * 100))}%"
    new_pct = f"{int(round(float(new) * 100))}%"
    for cat, label in (
        ("finder", "ML Finder"),
        ("strategy", "Стратегии"),
        ("grid_ft", "Grid"),
    ):
        append_entry(
            field="ml_gate_min_confidence",
            old=old_pct,
            new=new_pct,
            category=cat,
            source=source,
            label=f"ML gate profit ({label})",
        )


def record_pair_whitelist_change(action: str, pair: str) -> None:
    append_note(
        text=f"Whitelist ML Finder: пара {pair} {'добавлена' if action == 'add' else 'удалена'}",
        category="finder",
        source="ui",
    )


def record_strategy_toggle(strategy_id: str, enabled: bool) -> None:
    append_note(
        text=f"Стратегия {strategy_id}: {'включена' if enabled else 'выключена'}",
        category="strategy",
        source="ui",
    )


def record_ranging_scan_summary(summary: dict[str, Any]) -> None:
    pairs = summary.get("pairs") or []
    top = ", ".join(pairs[:5]) if pairs else "—"
    append_note(
        text=f"Скан боковика: найдено {summary.get('suitable_found', '?')} пар, whitelist обновлён. Топ: {top}",
        category="ranging_scanner",
        source="auto",
    )


def record_strategy_scan_summary(data: dict[str, Any]) -> None:
    wl = data.get("whitelist") or []
    top = ", ".join(wl[:5]) if wl else "—"
    append_note(
        text=(
            f"Скан пар стратегий: {data.get('selected_count', '?')} пар в whitelist. "
            f"Примеры: {top}"
        ),
        category="strategy_scanner",
        source="auto",
    )


def build_periods(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_field: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(entries, key=lambda x: x.get("at") or ""):
        field = e.get("field")
        if not field or field == "_note":
            continue
        by_field.setdefault(field, []).append(e)

    periods: dict[str, list[dict[str, Any]]] = {}
    for field, changes in by_field.items():
        rows: list[dict[str, Any]] = []
        for i, ch in enumerate(changes):
            until = changes[i + 1]["at"] if i + 1 < len(changes) else None
            rows.append(
                {
                    "from": ch["at"],
                    "until": until,
                    "value": ch.get("new"),
                    "value_fmt": _fmt_value(field, ch.get("new")),
                }
            )
        periods[field] = list(reversed(rows))
    return periods


def get_changelog_payload() -> dict[str, Any]:
    ensure_seeded()
    data = load_changelog()
    entries = list(data.get("entries") or [])
    return {
        "entries": entries,
        "periods": build_periods(entries),
        "count": len(entries),
        "categories": CATEGORY_LABELS,
    }


def _seed_rows() -> list[tuple[str, str, str, Any, Any, str | None]]:
    """at, category, field, old, new, note/text for _note."""
    return [
        # --- Telegram / Kronos ---
        ("2026-06-18T10:00:00+00:00", "telegram_bot", "_note", None, None,
         "Создан Telegram-бот: Chronos + Binance API, прогноз ALGO/BTC/ETH и др."),
        ("2026-06-19T14:00:00+00:00", "telegram_bot", "_note", None, None,
         "Стабильный прогноз: только закрытые свечи, seed на период, кэш до закрытия TF"),
        ("2026-06-20T08:00:00+00:00", "telegram_bot", "_note", None, None,
         "Таймфреймы 5m, 10m, 20m; скан 60 комбинаций (10 пар × 6 TF)"),
        ("2026-06-20T16:00:00+00:00", "telegram_bot", "_note", None, None,
         "Скан: облегчённый режим (lookback 128, 2 сэмпла), пагинация 1m для 10m/20m"),
        ("2026-06-20T18:00:00+00:00", "telegram_bot", "_note", None, None,
         "История сканов в БД (scan_runs), кнопка «Сканы» в боте"),
        ("2026-06-21T10:00:00+00:00", "telegram_bot", "_note", None, None,
         "Торговля Bybit из Telegram: spot long + perp short по сигналам скана"),
        ("2026-06-21T14:00:00+00:00", "telegram_bot", "_note", None, None,
         "Исправлена синхронизация времени с Bybit (ошибка 10002)"),
        ("2026-06-21T15:00:00+00:00", "telegram_bot", "_note", None, None,
         "Исправлен минимум ордера futures (110094), расчёт qty с minNotional"),
        # --- VPS ---
        ("2026-06-22T10:00:00+00:00", "finder", "_note", None, None,
         "Развёрнут CryptoTools на VPS 77.222.35.209, Bybit futures dry-run/live"),
        ("2026-06-22T12:00:00+00:00", "finder", "max_open_trades", None, "3",
         "Бот ML Finder (legacy)"),
        ("2026-06-22T12:00:00+00:00", "strategy", "max_open_trades", None, "2",
         "Бот стратегий (MultiStrategyRouter)"),
        ("2026-06-22T14:00:00+00:00", "ui_infra", "_note", None, None,
         "FreqUI + nginx HTTPS на порту 8443, самоподписанный сертификат"),
        ("2026-06-23T08:00:00+00:00", "ui_infra", "_note", None, None,
         "Панель CriptoTools: свой UI вместо FreqUI (2 колонки → 4 бота)"),
        ("2026-06-23T10:00:00+00:00", "strategy", "_note", None, None,
         "CriptoPairsStrategy: таймфрейм 1h → 5m"),
        ("2026-06-23T11:00:00+00:00", "strategy", "_note", None, None,
         "Добавлены стратегии: Supertrend, MACD, TripleEMA, BB+RSI, ADX; мульти-включение"),
        ("2026-06-23T14:00:00+00:00", "grid_ft", "max_open_trades", None, "2",
         "Третий бот: VolatilityGridStrategy (Grid)"),
        ("2026-06-23T16:00:00+00:00", "strategy_scanner", "_note", None, None,
         "Сканер пар под включённые стратегии (150 ликвидных, timer на VPS)"),
        # --- Ranging scanner v3 ---
        ("2026-06-24T08:00:00+00:00", "ranging_scanner", "_note", None, None,
         "Сканер боковика v3: ADX, BB width, EMA slope, ATR ratio, MTF 15m"),
        ("2026-06-24T10:00:00+00:00", "ranging_scanner", "adx_max", "22", "26",
         "Ослаблен фильтр тренда — больше кандидатов в трендовом рынке"),
        ("2026-06-24T10:00:00+00:00", "ranging_scanner", "max_ema50_slope", "0.0012", "0.004",
         "Главный блокер «0 пар» был ema_slope"),
        ("2026-06-24T10:00:00+00:00", "ranging_scanner", "min_ranging_ratio", "0.65", "0.55",
         "Soft-rank при 0 strict passes"),
        ("2026-06-24T12:00:00+00:00", "ranging_scanner", "_note", None, None,
         "Сканер на timer (systemd), whitelist → config_grid.json"),
        # --- Bybit Grid ---
        ("2026-06-24T14:00:00+00:00", "bybit_grid", "_note", None, None,
         "Четвёртый бот: нативный Bybit Futures Grid (Funding Account)"),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "stop_loss_usdt", None, "0.4", "Стартовые параметры"),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "take_profit_usdt", None, "0.4", None),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "cell_number", None, "10", None),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "price_range_pct", None, "0.06", "±6%"),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "total_investment", None, "10", "USDT на бота"),
        ("2026-06-24T12:00:00+00:00", "bybit_grid", "max_active_bots", None, "3", None),
        ("2026-06-24T20:00:00+00:00", "system", "_note", None, None,
         "Fix: SL precision ≤2 знака; ошибка 400001 Funding; импорт bot_id; sync UI↔Bybit"),
        ("2026-06-25T08:00:00+00:00", "system", "_note", None, None,
         "API rate limit: fgridbot 10/с, кэш detail 25 с, UI poll Bybit Grid 30 с"),
        ("2026-06-25T12:00:00+00:00", "bybit_grid", "stop_loss_usdt", "0.4", "0.55", "Пакет оптимизации"),
        ("2026-06-25T12:00:00+00:00", "bybit_grid", "take_profit_usdt", "0.4", "0.3", None),
        ("2026-06-25T12:00:00+00:00", "bybit_grid", "cell_number", "10", "15", None),
        ("2026-06-25T12:00:00+00:00", "bybit_grid", "price_range_pct", "0.06", "0.09", "±6% → ±9%"),
        ("2026-06-25T12:00:00+00:00", "deploy_guards", "neutral_max_1h_move_pct", None, "1.5",
         "A1: Neutral запрещён при |движение 1ч| > 1.5%"),
        ("2026-06-25T12:00:00+00:00", "deploy_guards", "sl_cooldown_hours", None, "3",
         "A3: пауза 3 ч на пару после SL"),
        ("2026-06-25T14:00:00+00:00", "ui_infra", "_note", None, None,
         "Кнопка «Изменения» — журнал настроек для сравнения с PnL"),
        # --- Optimization pack G/S/F (25 Jun evening) ---
        ("2026-06-25T18:00:00+00:00", "grid_ft", "stoploss", "-5%", "-3%", "G3: меньше ущерб от SL"),
        ("2026-06-25T18:00:00+00:00", "grid_ft", "stake_amount", "5", "3", "G6: USDT на сделку"),
        ("2026-06-25T18:00:00+00:00", "grid_ft", "max_open_trades", "2", "1", "G6: одна позиция"),
        ("2026-06-25T18:00:00+00:00", "grid_ft", "_note", None, None,
         "G1: ADX<22, BB width≥2.2%; G2: blacklist H/FOLKS/UB/BLESS/ESPORTS/FARTCOIN"),
        ("2026-06-25T18:00:00+00:00", "grid_ft", "_note", None, None,
         "G3: DCA отключён; G4: cooldown после SL 240 мин; G5: whitelist ≤5 пар из сканера"),
        ("2026-06-25T18:00:00+00:00", "strategy", "_note", None, None,
         "S1: только CriptoPairs + BollingerRsi; S2: вход при ADX<25"),
        ("2026-06-25T18:00:00+00:00", "strategy", "_note", None, None,
         "S4: use_exit_signal=false; S5: ROI 2% на 0 мин"),
        ("2026-06-25T18:00:00+00:00", "strategy_scanner", "_note", None, None,
         "S3: min turnover $8M, exclude мемы/HEI/SAHARA/SOXL"),
        ("2026-06-25T18:00:00+00:00", "finder", "max_open_trades", "3", "1", "F3: лимит позиций"),
        ("2026-06-25T18:00:00+00:00", "finder", "_note", None, None,
         "F2: PROB_THRESHOLD 0.55→0.62; F4: retrain каждые 4ч, train 15 дней"),
        ("2026-06-25T18:00:00+00:00", "finder", "_note", None, None,
         "F1: закрыты зависшие позиции DOGE/SOL/XRP с 21.06"),
        ("2026-06-26T12:00:00+00:00", "grid_ft", "_note", None, None,
         "Blacklist v2: GRASS, OPG, BEL, BILL, SLX, AERO, LUMIA, JUP, SPK, BLEND, AAVE (+ сканер/Bybit Grid)"),
        # --- ML prod pack (Jul 2026) ---
        ("2026-07-04T20:30:00+00:00", "finder", "_note", None, None,
         "ML Finder остановлен на VPS — заменён ML gate (sim +426 USDT vs −472 без gate, Jan 2025+)"),
        ("2026-07-04T20:30:00+00:00", "strategy", "_note", None, None,
         "Prod ML gate: XGBoost pnl_classifier в confirm_trade_entry · TripleEMA + BB+RSI + ADX · block_loss"),
        ("2026-07-04T20:30:00+00:00", "grid_ft", "_note", None, None,
         "Prod ML gate на VolatilityGridStrategy (сценарий live_grid) · block_loss"),
        ("2026-07-04T20:30:00+00:00", "strategy", "_note", None, None,
         "Sim: отключены слабые боты (ликвидность −10, свинг −2, HFT/скальп ≤+5 USDT) — 6 прибыльных с ML"),
        ("2026-07-04T20:30:00+00:00", "ui_infra", "_note", None, None,
         "Панель CriptoTools: подписи ML gate на Grid и Стратегиях; ML Finder помечен OFF"),
        # --- ML Finder replaces ML Finder (Jul 2026) ---
        ("2026-07-04T21:15:00+00:00", "finder", "_note", None, None,
         "ML Finder (TradeFinderStrategy + XGBoost scanner) включён на VPS вместо ML Finder · pnl gate block_loss"),
        ("2026-07-04T21:15:00+00:00", "ui_infra", "_note", None, None,
         "Панель CriptoTools: ML Finder → ML Finder, сервис Finder снова активен"),
        ("2026-07-05T12:00:00+00:00", "finder", "ml_gate_min_confidence", "80%", "60%",
         "profit_only — все входы ML Finder"),
        ("2026-07-05T12:00:00+00:00", "strategy", "ml_gate_min_confidence", "80%", "60%",
         "profit_only — MultiStrategyRouter"),
        ("2026-07-05T12:00:00+00:00", "grid_ft", "ml_gate_min_confidence", "80%", "60%",
         "profit_only — VolatilityGridStrategy"),
        ("2026-07-05T12:00:00+00:00", "ui_infra", "_note", None, None,
         "Панель: подписи ML gate 80% → 60%; симуляция all-pairs: +1544 USDT global ML @60%"),
    ]


def ensure_seeded() -> None:
    data = load_changelog()
    if int(data.get("seeded_version") or 0) >= SEED_VERSION:
        return
    existing = {(e.get("at"), e.get("field"), e.get("text")) for e in data.get("entries", [])}
    for at, cat, field, old, new, note in _seed_rows():
        if field == "_note":
            text = note or ""
        else:
            text = format_change_text(field, old, new)
            if note:
                text = f"{text} ({note})"
        key = (at, field, text)
        if key in existing:
            continue
        entry: dict[str, Any] = {
            "at": at,
            "category": cat,
            "field": field,
            "old": old,
            "new": new,
            "text": text,
            "source": "seed",
        }
        if note and field != "_note":
            entry["note"] = note
        data.setdefault("entries", []).append(entry)
        existing.add(key)
    data["entries"].sort(key=lambda x: x.get("at") or "", reverse=True)
    data["seeded_version"] = SEED_VERSION
    save_changelog(data)
