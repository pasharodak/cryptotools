#!/usr/bin/env python3
"""CryptoTools Telegram bot: Mini App (site) + /start with chat id for linking."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
SCRIPTS = BASE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import telegram_links as tg  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("telegram_bot")

OFFSET_PATH = BASE / "user_data" / "logs" / "telegram-bot.offset"

HELP_TEXT = (
    "📖 Команды бота CryptoTools:\n\n"
    "/start — приветствие, ваш chat id и кнопка мини-приложения\n"
    "/help — эта справка по командам\n"
    "/id — показать ваш chat id и статус привязки к сайту\n"
    "/panel — открыть панель CryptoTools (мини-приложение)\n"
    "/status — кратко: привязан ли аккаунт сайта\n\n"
    "Как получать уведомления о сделках:\n"
    "1) отправьте /start и скопируйте chat id\n"
    "2) на сайте → Настройки → Telegram → вставьте ID → Сохранить\n"
    "3) сюда будут приходить открытия/закрытия сделок на русском "
    "(в т.ч. плюс/минус в USDT)"
)


def _api(method: str, payload: dict | None = None) -> dict:
    return tg.api_call(method, payload)


def _save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def _load_offset() -> int:
    if not OFFSET_PATH.is_file():
        return 0
    try:
        return int(OFFSET_PATH.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def _webapp_keyboard() -> dict:
    url = tg.webapp_url()
    return {
        "keyboard": [
            [{"text": "🚀 Открыть CryptoTools", "web_app": {"url": url}}],
            [{"text": "🆔 Мой chat id"}, {"text": "📖 Команды"}],
        ],
        "resize_keyboard": True,
    }


def _setup_menu() -> None:
    url = tg.webapp_url()
    try:
        _api(
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "CryptoTools",
                    "web_app": {"url": url},
                }
            },
        )
        log.info("menu button → %s", url)
    except Exception as exc:
        log.warning("setChatMenuButton failed: %s", exc)
    try:
        _api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Старт, chat id и мини-приложение"},
                    {"command": "help", "description": "Справка по командам на русском"},
                    {"command": "id", "description": "Показать ваш Telegram chat id"},
                    {"command": "panel", "description": "Открыть панель CryptoTools"},
                    {"command": "status", "description": "Статус привязки к сайту"},
                ]
            },
        )
        log.info("bot commands (RU) registered")
    except Exception as exc:
        log.warning("setMyCommands failed: %s", exc)


def _send(chat_id: int | str, text: str, *, markdown: bool = False) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": _webapp_keyboard(),
        "disable_web_page_preview": True,
    }
    if markdown:
        payload["parse_mode"] = "Markdown"
    _api("sendMessage", payload)


def _handle_start(chat_id: int | str, first_name: str = "") -> None:
    name = (first_name or "").strip() or "друг"
    linked = tg.user_for_chat(str(chat_id))
    link_txt = (
        f"Привязан аккаунт сайта: «{linked}»."
        if linked
        else (
            "Чтобы получать сделки в этот чат:\n"
            "1) скопируйте chat id ниже\n"
            "2) на сайте → Настройки → Telegram → вставьте ID → Сохранить"
        )
    )
    text = (
        f"Привет, {name}!\n\n"
        f"Это бот CryptoTools.\n"
        f"Ваш chat id: `{chat_id}`\n\n"
        f"{link_txt}\n\n"
        "Кнопка ниже открывает панель как мини-приложение Telegram.\n"
        "Справка по командам: /help"
    )
    _send(chat_id, text, markdown=True)


def _handle_id(chat_id: int | str) -> None:
    linked = tg.user_for_chat(str(chat_id))
    extra = (
        f"\nАккаунт сайта: «{linked}» (уведомления включены)."
        if linked
        else "\nНа сайте пока не привязан — вставьте этот id в Настройки → Telegram."
    )
    _send(chat_id, f"Ваш chat id: `{chat_id}`{extra}", markdown=True)


def _handle_status(chat_id: int | str) -> None:
    linked = tg.user_for_chat(str(chat_id))
    if linked:
        link = tg.get_link(linked)
        en = "да" if link.get("enabled") else "нет"
        text = (
            f"Статус: привязан к аккаунту «{linked}».\n"
            f"Уведомления о сделках: {en}.\n"
            f"Chat id: `{chat_id}`"
        )
    else:
        text = (
            f"Статус: аккаунт сайта не привязан.\n"
            f"Chat id: `{chat_id}`\n"
            "Откройте /start и сохраните id в Настройках на сайте."
        )
    _send(chat_id, text, markdown=True)


def _handle_panel(chat_id: int | str) -> None:
    url = tg.webapp_url()
    _send(
        chat_id,
        f"Панель CryptoTools:\n{url}\n\n"
        "Или нажмите кнопку «Открыть CryptoTools» ниже "
        "(в Telegram откроется мини-приложение).",
    )


def _handle_update(upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = str(msg.get("text") or "").strip()
    user = msg.get("from") or {}
    first = str(user.get("first_name") or "")
    low = text.lower()

    if low.startswith("/start"):
        _handle_start(chat_id, first)
        return
    if low.startswith("/help") or text in ("📖 Команды", "Команды", "команды"):
        _send(chat_id, HELP_TEXT)
        return
    if text in ("🆔 Мой chat id",) or low.startswith("/id") or low.startswith("/chatid"):
        _handle_id(chat_id)
        return
    if low.startswith("/status"):
        _handle_status(chat_id)
        return
    if low.startswith("/panel") or low.startswith("/app"):
        _handle_panel(chat_id)
        return
    if text.startswith("/"):
        _send(
            chat_id,
            "Неизвестная команда.\n\n" + HELP_TEXT,
        )


def main() -> int:
    if not tg.bot_token():
        log.error("TELEGRAM_BOT_TOKEN missing")
        return 2
    me = _api("getMe")
    username = ((me.get("result") or {}).get("username")) or "?"
    log.info("bot @%s webapp=%s", username, tg.webapp_url())
    _setup_menu()
    offset = _load_offset()
    while True:
        try:
            data = _api(
                "getUpdates",
                {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
            )
        except Exception as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(3)
            continue
        for upd in data.get("result") or []:
            try:
                _handle_update(upd)
            except Exception as exc:
                log.exception("update failed: %s", exc)
            uid = int(upd.get("update_id") or 0)
            offset = uid + 1
            _save_offset(offset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
