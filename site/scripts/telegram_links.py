#!/usr/bin/env python3
"""Per-user Telegram chat linking + outbound Bot API notifications (RU)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tenant_manager as tm

log = logging.getLogger("telegram_links")

LINKS_PATH = tm.BASE / "user_data" / "telegram_links.json"  # legacy; migrated → DB
SETTINGS_DB_KEY = "telegram_links"
DEFAULT_WEBAPP_URL = "https://194.163.149.122:8443/"

# Stable keys for UI + notify filter (order = display order).
NOTIFY_KIND_META: list[tuple[str, str]] = [
    ("trade_open", "Открытия сделки"),
    ("trade_close_profit", "Закрытие сделки (только успешные)"),
    ("trade_close_loss", "Закрытие сделки (только минусовые)"),
    ("trade_close_all", "Закрытие сделки (все)"),
    ("bot_start", "Запуск ботов"),
    ("bot_stop", "Остановка ботов"),
    ("bot_reload", "Перезапуск конфигурации"),
    ("other", "Другие"),
]
NOTIFY_KIND_KEYS = tuple(k for k, _ in NOTIFY_KIND_META)
DEFAULT_NOTIFY: dict[str, bool] = {
    "trade_open": True,
    "trade_close_profit": True,
    "trade_close_loss": True,
    "trade_close_all": True,
    "bot_start": False,
    "bot_stop": False,
    "bot_reload": False,
    "other": True,
}


def bot_token() -> str:
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def normalize_notify(raw: Any = None) -> dict[str, bool]:
    out = dict(DEFAULT_NOTIFY)
    if isinstance(raw, dict):
        for key in NOTIFY_KIND_KEYS:
            if key in raw:
                out[key] = bool(raw[key])
    # «Все закрытия» покрывает profit/loss — для UI держим согласованность.
    if out.get("trade_close_all"):
        out["trade_close_profit"] = True
        out["trade_close_loss"] = True
    return out


def notify_kinds_catalog() -> list[dict[str, Any]]:
    prefs = DEFAULT_NOTIFY
    return [
        {"id": key, "label": label, "default": bool(prefs.get(key, False))}
        for key, label in NOTIFY_KIND_META
    ]


def webapp_url() -> str:
    raw = (
        os.environ.get("TELEGRAM_WEBAPP_URL")
        or os.environ.get("PUBLIC_SITE_URL")
        or DEFAULT_WEBAPP_URL
    ).strip()
    if raw and not raw.endswith("/"):
        raw += "/"
    return raw or DEFAULT_WEBAPP_URL


def _empty() -> dict[str, Any]:
    return {"by_user": {}, "by_chat": {}}


def _control_plane():
    import control_plane as cp

    return cp


def _normalize_store(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty()
    by_user = raw.get("by_user") if isinstance(raw.get("by_user"), dict) else {}
    by_chat = raw.get("by_chat") if isinstance(raw.get("by_chat"), dict) else {}
    # Normalize per-user notify prefs so old rows stay valid.
    clean_users: dict[str, Any] = {}
    for uid, row in by_user.items():
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["notify"] = normalize_notify(item.get("notify"))
        clean_users[str(uid)] = item
    clean_chat = {str(k): str(v) for k, v in by_chat.items() if k and v}
    return {"by_user": clean_users, "by_chat": clean_chat}


def _load_links_json() -> dict[str, Any] | None:
    if not LINKS_PATH.is_file():
        return None
    try:
        data = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_store(data)


def _migrate_json_to_db() -> dict[str, Any] | None:
    payload = _load_links_json()
    if not payload or (not payload["by_user"] and not payload["by_chat"]):
        return None
    try:
        _control_plane().set_app_setting(
            SETTINGS_DB_KEY,
            payload,
            updated_by="migrate_json",
        )
        try:
            LINKS_PATH.rename(LINKS_PATH.with_suffix(".json.migrated"))
        except OSError:
            pass
        log.info("migrated telegram_links.json → control_plane.sqlite app_settings")
        return payload
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_links migrate failed: %s", exc)
        return payload


def load_links() -> dict[str, Any]:
    """Load links from control_plane.sqlite (app_settings); migrate legacy JSON once."""
    cp = _control_plane()
    try:
        raw = cp.get_app_setting(SETTINGS_DB_KEY, None)
    except Exception as exc:  # noqa: BLE001
        log.debug("telegram app_settings read failed: %s", exc)
        raw = None
    if isinstance(raw, dict):
        return _normalize_store(raw)
    migrated = _migrate_json_to_db()
    if migrated is not None:
        return migrated
    return _empty()


def save_links(data: dict[str, Any], *, updated_by: str | None = None) -> None:
    payload = _normalize_store(data)
    _control_plane().set_app_setting(
        SETTINGS_DB_KEY,
        payload,
        updated_by=updated_by or "api",
    )


def normalize_chat_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("@"):
        return s
    # Allow -100… group ids and positive user ids
    if s[0] == "-" and s[1:].isdigit():
        return s
    if s.isdigit():
        return s
    raise ValueError("chat_id должен быть числом (из /start бота) или @username")


def get_link(user_id: str) -> dict[str, Any]:
    try:
        import tenant_manager as tm

        owner_id = tm.telegram_owner_id(str(user_id))
    except Exception:
        owner_id = str(user_id)
    data = load_links()
    row = data["by_user"].get(str(owner_id)) or {}
    if not isinstance(row, dict):
        row = {}
    chat_id = str(row.get("chat_id") or "").strip()
    notify = normalize_notify(row.get("notify"))
    return {
        "user_id": str(user_id),
        "owner_user_id": str(owner_id),
        "chat_id": chat_id,
        "enabled": bool(row.get("enabled", True)) if chat_id else False,
        "notify": notify,
        "notify_kinds": notify_kinds_catalog(),
        "linked_at": row.get("linked_at"),
        "bot_configured": bool(bot_token()),
        "webapp_url": webapp_url(),
        "stored_in": "control_plane.sqlite",
    }


def set_link(
    user_id: str,
    chat_id: str,
    *,
    enabled: bool = True,
    notify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import tenant_manager as tm

        uid = tm.telegram_owner_id(str(user_id))
    except Exception:
        uid = str(user_id)
    cid = normalize_chat_id(chat_id) if str(chat_id or "").strip() else ""
    data = load_links()
    # Drop old reverse map for this user
    old = data["by_user"].get(uid) or {}
    if not isinstance(old, dict):
        old = {}
    old_cid = str(old.get("chat_id") or "").strip()
    if old_cid and data["by_chat"].get(old_cid) == uid:
        data["by_chat"].pop(old_cid, None)
    if not cid:
        data["by_user"].pop(uid, None)
        save_links(data)
        return get_link(uid)
    # One chat → one CryptoTools user (parent / live id)
    prev_uid = data["by_chat"].get(cid)
    if prev_uid and prev_uid != uid:
        data["by_user"].pop(prev_uid, None)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Merge notify: PATCH may omit → keep previous / defaults
    if notify is None:
        merged_notify = normalize_notify(old.get("notify"))
    else:
        base = normalize_notify(old.get("notify"))
        if isinstance(notify, dict):
            base.update({k: bool(notify[k]) for k in NOTIFY_KIND_KEYS if k in notify})
        merged_notify = normalize_notify(base)
    data["by_user"][uid] = {
        "chat_id": cid,
        "enabled": bool(enabled),
        "notify": merged_notify,
        "linked_at": old.get("linked_at") or now,
        "updated_at": now,
    }
    data["by_chat"][cid] = uid
    save_links(data)
    return get_link(uid)


def user_for_chat(chat_id: str) -> str | None:
    try:
        cid = normalize_chat_id(chat_id)
    except ValueError:
        return None
    data = load_links()
    uid = data["by_chat"].get(cid)
    return str(uid) if uid else None


def api_call(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = bot_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # getUpdates long-poll uses payload["timeout"] (e.g. 25s); HTTP client must wait longer.
    poll_s = 0
    if isinstance(payload, dict) and payload.get("timeout") is not None:
        try:
            poll_s = int(payload["timeout"])
        except (TypeError, ValueError):
            poll_s = 0
    http_timeout = max(30, poll_s + 15)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {method} HTTP {exc.code}: {err_body[:300]}") from exc


def send_message(chat_id: str, text: str, *, disable_preview: bool = True) -> bool:
    try:
        cid = normalize_chat_id(chat_id)
    except ValueError:
        return False
    if not bot_token() or not text.strip():
        return False
    try:
        api_call(
            "sendMessage",
            {
                "chat_id": cid,
                "text": text,
                "disable_web_page_preview": disable_preview,
            },
        )
        return True
    except Exception as exc:
        log.warning("telegram send chat=%s failed: %s", cid, exc)
        return False


def allows_notify(
    user_id: str,
    kind: str,
    *,
    pnl_pct: float | None = None,
) -> bool:
    """Whether this user wants ``kind`` notifications (and master enabled)."""
    link = get_link(user_id)
    if not link.get("enabled") or not link.get("chat_id"):
        return False
    prefs = normalize_notify(link.get("notify"))
    key = str(kind or "other").strip() or "other"

    if key in ("trade_close", "trade_close_all", "trade_close_profit", "trade_close_loss"):
        if prefs.get("trade_close_all"):
            return True
        if key == "trade_close_all":
            return True
        if pnl_pct is None:
            # Unknown PnL: only if at least one close bucket is on.
            return bool(
                prefs.get("trade_close_profit")
                or prefs.get("trade_close_loss")
                or prefs.get("trade_close_all")
            )
        if float(pnl_pct) >= 0:
            return bool(prefs.get("trade_close_profit"))
        return bool(prefs.get("trade_close_loss"))

    if key not in NOTIFY_KIND_KEYS:
        key = "other"
    return bool(prefs.get(key))


def notify_user(
    user_id: str,
    text: str,
    *,
    kind: str = "other",
    pnl_pct: float | None = None,
) -> bool:
    if not allows_notify(user_id, kind, pnl_pct=pnl_pct):
        return False
    link = get_link(user_id)
    return send_message(str(link["chat_id"]), text)


def format_bot_event_ru(*, action: str, bot: str, user_id: str = "", detail: str = "") -> str:
    labels = {
        "start": ("▶️ Запуск бота", "bot_start"),
        "stop": ("⏹ Остановка бота", "bot_stop"),
        "reload": ("🔄 Перезапуск конфигурации", "bot_reload"),
    }
    title, _ = labels.get(action, ("ℹ️ Событие бота", "other"))
    bot_names = {
        "strategy": "Strategy",
        "grid": "Grid",
        "finder": "ML Finder",
        "signal-engine": "Signal Engine",
        "trade-executor": "Executor",
    }
    lines = [title, f"Бот: {bot_names.get(bot, bot or '—')}"]
    if user_id:
        lines.append(f"Аккаунт: {user_id}")
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def _side_ru(side: str, *, is_short: bool | None = None) -> str:
    s = (side or "").lower()
    if is_short is True or s in ("short", "sell"):
        return "шорт"
    if is_short is False or s in ("long", "buy"):
        return "лонг"
    return side or "?"


def _exit_reason_ru(reason: str) -> str:
    mapping = {
        "roi_take_profit": "тейк-профит (ROI)",
        "stop_loss": "стоп-лосс",
        "exchange_flat": "позиция закрыта на бирже",
        "force_exit": "принудительный выход",
        "trailing_stop_loss": "трейлинг-стоп",
        "exit_signal": "сигнал выхода",
        "custom_exit": "выход стратегии",
    }
    return mapping.get(reason, reason or "неизвестно")


# Bybit USDT-perp taker fee — same as custom-ui / pair_config_server (0.1% open + 0.1% close).
BYBIT_TAKER_FEE_RATE = 0.001


def round_trip_fees(
    *,
    amount: float | None = None,
    open_rate: float | None = None,
    close_rate: float | None = None,
    stake_amount: float | None = None,
    leverage: float | None = None,
) -> float:
    """Estimate round-trip taker fees in USDT (matches UI «С комиссией»)."""
    fee = BYBIT_TAKER_FEE_RATE
    try:
        amt = float(amount or 0)
        open_px = float(open_rate or 0)
        close_px = float(close_rate or 0)
    except (TypeError, ValueError):
        amt, open_px, close_px = 0.0, 0.0, 0.0
    if amt > 0 and open_px > 0:
        close_px = close_px if close_px > 0 else open_px
        return amt * open_px * fee + amt * close_px * fee
    try:
        stake = float(stake_amount or 0)
        lev = float(leverage or 1) or 1.0
    except (TypeError, ValueError):
        stake, lev = 0.0, 1.0
    if stake > 0:
        return 2.0 * stake * lev * fee
    return 0.0


def pnl_with_fees(
    profit_abs: float,
    *,
    amount: float | None = None,
    open_rate: float | None = None,
    close_rate: float | None = None,
    stake_amount: float | None = None,
    leverage: float | None = None,
    profit_pct: float | None = None,
) -> dict[str, float]:
    """Gross PnL → net after round-trip fees. Keys: gross, fees, net, net_pct, gross_pct."""
    try:
        gross = float(profit_abs or 0)
    except (TypeError, ValueError):
        gross = 0.0
    fees = round_trip_fees(
        amount=amount,
        open_rate=open_rate,
        close_rate=close_rate,
        stake_amount=stake_amount,
        leverage=leverage,
    )
    net = gross - fees
    try:
        stake = float(stake_amount or 0)
    except (TypeError, ValueError):
        stake = 0.0
    if stake > 0:
        net_pct = (net / stake) * 100.0
        gross_pct = (gross / stake) * 100.0
    else:
        try:
            gross_pct = float(profit_pct if profit_pct is not None else 0)
        except (TypeError, ValueError):
            gross_pct = 0.0
        # Without stake, approximate net % from gross % by fee share if gross != 0.
        if abs(gross) > 1e-12:
            net_pct = gross_pct * (net / gross)
        else:
            net_pct = gross_pct
    return {
        "gross": gross,
        "fees": fees,
        "net": net,
        "net_pct": net_pct,
        "gross_pct": gross_pct,
    }


def format_entry_ru(
    *,
    pair: str,
    side: str,
    strategy: str,
    price: float,
    stake: float,
    qty: float | None = None,
    demo: bool = False,
    order_id: str | None = None,
) -> str:
    lines = [
        "🟢 Открыта сделка",
        f"Пара: {pair}",
        f"Сторона: {_side_ru(side)}",
        f"Стратегия: {strategy or '—'}",
        f"Цена: {price:.6g}",
        f"Стейк: {stake:.2f} USDT",
    ]
    if qty is not None and qty > 0:
        lines.append(f"Объём: {qty:.6g}")
    lines.append(f"Режим: {'Demo' if demo else 'Live'}")
    if order_id:
        lines.append(f"Ордер: {order_id}")
    return "\n".join(lines)


def format_exit_ru(
    *,
    pair: str,
    strategy: str,
    reason: str,
    pnl_pct: float,
    is_short: bool = False,
    close_rate: float | None = None,
    pnl_usdt: float | None = None,
    stake: float | None = None,
    amount: float | None = None,
    open_rate: float | None = None,
    leverage: float | None = None,
) -> str:
    if pnl_usdt is None and stake is not None:
        try:
            pnl_usdt = float(pnl_pct) / 100.0 * float(stake)
        except (TypeError, ValueError):
            pnl_usdt = None
    fee_info = pnl_with_fees(
        float(pnl_usdt or 0),
        amount=amount,
        open_rate=open_rate,
        close_rate=close_rate,
        stake_amount=stake,
        leverage=leverage,
        profit_pct=pnl_pct,
    )
    net_pct = fee_info["net_pct"]
    net_usdt = fee_info["net"]
    fees = fee_info["fees"]
    emoji = "🟢" if net_usdt >= 0 else "🔴"
    lines = [
        f"{emoji} Закрыта сделка",
        f"Пара: {pair}",
        f"Сторона: {_side_ru('', is_short=is_short)}",
        f"Стратегия: {strategy or '—'}",
        f"Причина: {_exit_reason_ru(reason)}",
        f"С комиссией: {net_pct:+.2f}% · {net_usdt:+.2f} USDT",
    ]
    if pnl_usdt is not None:
        lines.append(f"Без комиссии: {float(pnl_usdt):+.2f} USDT")
    if fees > 0:
        lines.append(f"Комиссия: ≈{fees:.2f} USDT (0.1%×2)")
    if close_rate is not None and close_rate > 0:
        lines.append(f"Цена выхода: {close_rate:.6g}")
    return "\n".join(lines)


def format_order_fail_ru(
    *,
    pair: str,
    side: str,
    strategy: str,
    error: str,
) -> str:
    return (
        "⚠️ Не удалось открыть сделку\n"
        f"Пара: {pair}\n"
        f"Сторона: {_side_ru(side)}\n"
        f"Стратегия: {strategy or '—'}\n"
        f"Ошибка: {error}"
    )


def validate_webapp_init_data(init_data: str) -> dict[str, Any] | None:
    """Validate Telegram WebApp initData; return parsed user dict or None."""
    token = bot_token()
    if not token or not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    auth_date = int(parsed.get("auth_date") or 0)
    if auth_date and abs(time.time() - auth_date) > 86400:
        return None
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    return user if isinstance(user, dict) else None
