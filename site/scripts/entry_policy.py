"""Whether a signal may open a trade.

``evaluate_entry`` is the seam. Order placement, pair claim, and exchange
adapters stay in trade_executor and run only after an ``Allowed`` result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Skipped:
    reason: str
    quiet: bool


def is_quiet_skip(reason: str) -> bool:
    """Exchange not selected for this panel — expected; don't spam SKIP lines."""
    text = (reason or "").lower()
    return "биржа" in text and "выключена" in text


def evaluate_entry(user_id: str, ev: dict[str, Any]) -> Allowed | Skipped:
    import pair_entry_guard
    import tenant_manager as tm
    import trade_executor as te
    import user_trading

    sid = str(ev.get("strategy_id") or "")
    pair = str(ev.get("pair") or "")
    side = str(ev.get("side") or "long")
    bot = str(ev.get("bot") or "")
    reason: str | None
    if bot in user_trading.RETIRED_BOTS:
        reason = f"{bot} выведен из торговли"
    elif bot == "llm":
        reason = te._llm_skip_reason(user_id, ev)
    elif bot == "llm50":
        reason = te._llm50_skip_reason(user_id, ev)
    elif bot == "cursor":
        reason = te._cursor_skip_reason(user_id, ev)
    elif bot == "cursor50":
        reason = te._cursor50_skip_reason(user_id, ev)
    elif not sid:
        reason = "нет strategy_id в сигнале"
    else:
        reason = _active_strategy_skip(user_id, ev, sid, pair, side, te, tm, user_trading, pair_entry_guard)
    if reason is None:
        return Allowed()
    return Skipped(reason=reason, quiet=is_quiet_skip(reason))


def _active_strategy_skip(
    user_id: str,
    ev: dict[str, Any],
    sid: str,
    pair: str,
    side: str,
    te: Any,
    tm: Any,
    user_trading: Any,
    pair_entry_guard: Any,
) -> str | None:
    enabled = te.load_enabled(user_id)
    if not enabled:
        return f"пустой enabled_strategies для {user_id}"
    base_sid = te._strategy_base_id(sid)
    if not enabled.get(base_sid, False) and not enabled.get(sid, False):
        return f"стратегия {base_sid or sid} выключена у пользователя"
    allowed_side = te.load_entry_sides(user_id).get(base_sid, "both")
    blocked_side = te.entry_side_skip(allowed_side, side, base_sid or sid)
    if blocked_side:
        return blocked_side
    if base_sid in te.ADX_LONG_ONLY_TAGS and side.lower() in ("short", "sell"):
        return f"{base_sid}: shorts отключены (только long)"
    if not pair:
        return "нет pair в сигнале"
    blocked = te.pair_block_reason(user_id, sid, pair)
    if blocked:
        return blocked

    limits = te.load_limits(user_id)
    test_ids = te.load_test_strategy_ids(user_id)
    panel_sid = base_sid or sid
    is_test_panel = panel_sid in test_ids
    owner = tm.parent_user_id_of_context(user_id)
    ctx_ex = tm.context_exchange(user_id) if user_id != "admin" else "bybit"
    try:
        import user_prefs as up

        trade_panels = up.load_trade_on_panels(owner)
    except Exception:
        trade_panels = {
            "trade_on_main": tm.DEFAULT_TRADE_ON,
            "trade_on_test": tm.DEFAULT_TRADE_ON,
        }

    if is_test_panel:
        if not te.uses_demo_api(user_id):
            return "тестовые стратегии только на Demo API"
        allowed_ex = set(
            user_trading.exchanges_for_trade_on(
                str(trade_panels.get("trade_on_test") or tm.DEFAULT_TRADE_ON)
            )
        )
        if ctx_ex not in allowed_ex:
            return (
                f"тестовые: биржа {ctx_ex} выключена "
                f"(trade_on_test={trade_panels.get('trade_on_test')})"
            )
        test_cap = int(limits.get("test_max_open_trades") or 0)
        if test_cap <= 0:
            return "max_open_trades=0 для тестовых (лимит выключен/ноль)"
        test_open = te.count_open_by_panel(user_id, test=True)
        if test_open >= test_cap:
            return f"лимит тестовых стратегий {test_open}/{test_cap}"
        per = int(limits.get("test_max_per_strategy") or 0)
    else:
        allowed_ex = set(
            user_trading.exchanges_for_trade_on(
                str(trade_panels.get("trade_on_main") or tm.DEFAULT_TRADE_ON)
            )
        )
        if ctx_ex not in allowed_ex:
            return (
                f"основные: биржа {ctx_ex} выключена "
                f"(trade_on_main={trade_panels.get('trade_on_main')})"
            )
        main_cap = int(limits.get("max_open_trades") or 0)
        if main_cap <= 0:
            return "max_open_trades=0 (лимит сделок выключен/ноль)"
        main_open = te.count_open_by_panel(user_id, test=False)
        if main_open >= main_cap:
            return f"лимит открытых сделок {main_open}/{main_cap}"
        per = limits["max_per_strategy"]

    if te.count_open_on_pair_by_panel(user_id, pair, test=is_test_panel) > 0:
        panel_label = "тестовых" if is_test_panel else "основных"
        return f"уже есть открытая сделка по {pair} в БД ({panel_label})"
    try:
        if te.exchange_has_position(user_id, pair, side):
            return f"на бирже уже есть позиция {side} по {pair}"
    except Exception as exc:
        return f"не удалось проверить позиции на бирже: {exc}"
    busy = pair_entry_guard.busy_reason(
        pair,
        side,
        bot="strategy",
        tenant_id=user_id,
        check_bybit=(user_id == "admin"),
    )
    if busy:
        return busy
    if per > 0:
        tag_n = te.count_open_trades(user_id, tag=sid)
        if tag_n >= per:
            return f"лимит на стратегию {sid}: {tag_n}/{per}"
    if not te.ml_allows(user_id, ev):
        raw = ev.get("ml") if isinstance(ev.get("ml"), dict) else {}
        min_conf = te._ml_min_conf(
            user_id, sid, ev.get("scenario") if isinstance(ev.get("scenario"), dict) else None
        )
        return (
            f"ML gate отклонил: pred={raw.get('predicted') or '?'} "
            f"profit_conf={float(raw.get('confidence_profit') or 0)*100:.1f}% "
            f"min={min_conf*100:.0f}%"
        )
    return None
