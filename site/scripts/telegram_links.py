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

LINKS_PATH = tm.BASE / "user_data" / "telegram_links.json"
DEFAULT_WEBAPP_URL = "https://77.222.35.209:8443/"


def bot_token() -> str:
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


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


def load_links() -> dict[str, Any]:
    if not LINKS_PATH.is_file():
        return _empty()
    try:
        data = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    by_user = data.get("by_user") if isinstance(data.get("by_user"), dict) else {}
    by_chat = data.get("by_chat") if isinstance(data.get("by_chat"), dict) else {}
    return {"by_user": by_user, "by_chat": by_chat}


def save_links(data: dict[str, Any]) -> None:
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINKS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(LINKS_PATH, 0o600)
    except OSError:
        pass


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
    data = load_links()
    row = data["by_user"].get(str(user_id)) or {}
    if not isinstance(row, dict):
        row = {}
    chat_id = str(row.get("chat_id") or "").strip()
    return {
        "user_id": str(user_id),
        "chat_id": chat_id,
        "enabled": bool(row.get("enabled", True)) if chat_id else False,
        "linked_at": row.get("linked_at"),
        "bot_configured": bool(bot_token()),
        "webapp_url": webapp_url(),
    }


def set_link(user_id: str, chat_id: str, *, enabled: bool = True) -> dict[str, Any]:
    uid = str(user_id)
    cid = normalize_chat_id(chat_id) if str(chat_id or "").strip() else ""
    data = load_links()
    # Drop old reverse map for this user
    old = data["by_user"].get(uid) or {}
    old_cid = str(old.get("chat_id") or "").strip()
    if old_cid and data["by_chat"].get(old_cid) == uid:
        data["by_chat"].pop(old_cid, None)
    if not cid:
        data["by_user"].pop(uid, None)
        save_links(data)
        return get_link(uid)
    # One chat → one CryptoTools user
    prev_uid = data["by_chat"].get(cid)
    if prev_uid and prev_uid != uid:
        data["by_user"].pop(prev_uid, None)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["by_user"][uid] = {
        "chat_id": cid,
        "enabled": bool(enabled),
        "linked_at": now,
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
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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


def notify_user(user_id: str, text: str) -> bool:
    link = get_link(user_id)
    if not link.get("enabled") or not link.get("chat_id"):
        return False
    return send_message(str(link["chat_id"]), text)


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
) -> str:
    emoji = "🟢" if pnl_pct >= 0 else "🔴"
    if pnl_usdt is None and stake is not None:
        try:
            pnl_usdt = float(pnl_pct) / 100.0 * float(stake)
        except (TypeError, ValueError):
            pnl_usdt = None
    lines = [
        f"{emoji} Закрыта сделка",
        f"Пара: {pair}",
        f"Сторона: {_side_ru('', is_short=is_short)}",
        f"Стратегия: {strategy or '—'}",
        f"Причина: {_exit_reason_ru(reason)}",
        f"Результат: {pnl_pct:+.2f}%",
    ]
    if pnl_usdt is not None:
        lines.append(f"В валюте: {pnl_usdt:+.2f} USDT")
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
