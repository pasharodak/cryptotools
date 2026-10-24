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
    "/status — открытые сделки (список)\n"
    "/status table — открытые сделки таблицей\n"
    "/profit — сводка за сутки: хорошие/плохие, без комиссии и с комиссией\n\n"
    "Как получать уведомления о сделках:\n"
    "1) отправьте /start и скопируйте chat id\n"
    "2) на сайте → Настройки → Telegram → вставьте ID → Сохранить\n"
    "3) сюда будут приходить открытия/закрытия сделок на русском "
    "(PnL с комиссией Bybit 0.1%×2)"
)

BOT_LABELS = {"strategy": "Strategy", "grid": "Grid", "finder": "Finder"}
MAX_TG_MSG = 3900


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
                    {"command": "status", "description": "Открытые сделки (/status table — таблица)"},
                    {"command": "profit", "description": "Сводка за сутки с комиссией"},
                ]
            },
        )
        log.info("bot commands (RU) registered")
    except Exception as exc:
        log.warning("setMyCommands failed: %s", exc)


def _send(
    chat_id: int | str,
    text: str,
    *,
    markdown: bool = False,
    html: bool = False,
) -> None:
    # Telegram hard limit ~4096; split long replies.
    chunks: list[str] = []
    body = text or ""
    while body:
        if len(body) <= MAX_TG_MSG:
            chunks.append(body)
            break
        cut = body.rfind("\n", 0, MAX_TG_MSG)
        if cut < 200:
            cut = MAX_TG_MSG
        chunks.append(body[:cut])
        body = body[cut:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        payload: dict = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if i == 0:
            payload["reply_markup"] = _webapp_keyboard()
        if html:
            payload["parse_mode"] = "HTML"
        elif markdown:
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


def _db_paths_for_user(user_id: str) -> dict[str, list[Path]]:
    """Same DB sources the web panel uses (executor strategy + grid + finder)."""
    uid = str(user_id)
    strategy: list[Path] = []
    try:
        import trade_executor as te

        strategy.append(te.db_path(uid))
    except Exception as exc:
        log.debug("executor db path: %s", exc)
    if uid == "admin":
        strategy.extend(
            [
                BASE / "user_data" / "tradesv3-strategy.sqlite",
                BASE / "tradesv3-strategy.sqlite",
            ]
        )
        grid = [
            BASE / "user_data" / "tradesv3-grid.sqlite",
            BASE / "tradesv3-grid.sqlite",
        ]
        finder = [
            BASE / "user_data" / "tradesv3-finder.sqlite",
            BASE / "tradesv3-finder.sqlite",
        ]
    else:
        tdir = BASE / "user_data" / "tenants" / uid
        strategy.append(tdir / "tradesv3-strategy.sqlite")
        grid = [tdir / "tradesv3-grid.sqlite"]
        finder = [tdir / "tradesv3-finder.sqlite", tdir / "tradesv3.sqlite"]
    return {"strategy": strategy, "grid": grid, "finder": finder}


def _read_open_trades(db_path: Path) -> list[dict]:
    import sqlite3

    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, pair, is_short, enter_tag, strategy, open_rate,
                       stake_amount, open_date, amount
                FROM trades
                WHERE is_open = 1
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("open trades %s: %s", db_path, exc)
        return []


def _read_closed_today(db_path: Path, day_start: str, day_end: str) -> list[dict]:
    import sqlite3

    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(trades)")}
            extra = ""
            if "amount" in cols:
                extra += ", amount"
            if "leverage" in cols:
                extra += ", leverage"
            rows = conn.execute(
                f"""
                SELECT id, pair, is_short, enter_tag, strategy,
                       open_rate, close_rate, stake_amount{extra},
                       close_profit, close_profit_abs, realized_profit,
                       close_date, exit_reason
                FROM trades
                WHERE is_open = 0
                  AND close_date IS NOT NULL
                  AND close_date >= ?
                  AND close_date < ?
                ORDER BY close_date DESC
                """,
                (day_start, day_end),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("closed today %s: %s", db_path, exc)
        return []


def _collect_bot_trades(
    user_id: str,
    reader,
) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for bot, paths in _db_paths_for_user(user_id).items():
        chosen: Path | None = None
        for p in paths:
            if p.is_file():
                chosen = p
                break
        if chosen is None:
            continue
        for r in reader(chosen):
            tid = int(r.get("id") or 0)
            key = (bot, tid)
            if tid and key in seen:
                continue
            if tid:
                seen.add(key)
            item = dict(r)
            item["bot"] = bot
            out.append(item)
    return out


def _collect_open_trades(user_id: str) -> list[dict]:
    return _collect_bot_trades(user_id, _read_open_trades)


def _today_bounds_local() -> tuple[str, str, str]:
    """Return (day_label, start_iso, end_iso) for local calendar day."""
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    label = start.strftime("%Y-%m-%d")
    # SQLite close_date is usually "YYYY-MM-DD HH:MM:SS[.mmm]" (naive UTC or local).
    # Match both by date prefix and full-day window.
    return label, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _collect_closed_today(user_id: str) -> tuple[str, list[dict]]:
    from datetime import datetime, timedelta

    label, _, _ = _today_bounds_local()
    # Also include UTC day window — DB timestamps are often UTC.
    now_utc = datetime.utcnow()
    utc_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_end = utc_start + timedelta(days=1)
    local_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    # Use the earlier start / later end so "today" covers local and UTC storage.
    day_start = min(utc_start, local_start).strftime("%Y-%m-%d %H:%M:%S")
    day_end = max(utc_end, local_end).strftime("%Y-%m-%d %H:%M:%S")

    def _reader(path: Path) -> list[dict]:
        return _read_closed_today(path, day_start, day_end)

    trades = _collect_bot_trades(user_id, _reader)
    # Deduplicate by filtering to calendar day label in close_date when possible
    filtered: list[dict] = []
    utc_label = utc_start.strftime("%Y-%m-%d")
    for t in trades:
        cd = str(t.get("close_date") or "")
        if cd.startswith(label) or cd.startswith(utc_label):
            filtered.append(t)
        elif cd >= day_start and cd < day_end:
            filtered.append(t)
    return label, filtered


def _fmt_rate(rate) -> str:
    if rate in (None, ""):
        return "—"
    try:
        return f"{float(rate):.6g}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(val, *, signed: bool = False) -> str:
    try:
        n = float(val or 0)
    except (TypeError, ValueError):
        n = 0.0
    if signed:
        return f"{n:+.2f}"
    return f"{n:.2f}"


def _escape_html(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_status_trades(trades: list[dict]) -> str:
    if not trades:
        return "Открытых сделок нет."
    lines = [f"Открыто сделок: {len(trades)}"]
    for t in trades[:25]:
        bot = BOT_LABELS.get(str(t.get("bot")), str(t.get("bot") or "?"))
        pair = t.get("pair") or "?"
        side = "шорт" if t.get("is_short") else "лонг"
        tag = t.get("enter_tag") or t.get("strategy") or "—"
        stake = float(t.get("stake_amount") or 0)
        lines.append(
            f"• [{bot}] #{t.get('id')} {pair} {side}\n"
            f"  {tag} · вход {_fmt_rate(t.get('open_rate'))} · стейк {stake:.2f} USDT"
        )
    if len(trades) > 25:
        lines.append(f"… и ещё {len(trades) - 25}")
    return "\n".join(lines)


def _format_status_table(trades: list[dict]) -> str:
    if not trades:
        return "Открытых сделок нет."
    rows = [["№", "Бот", "Пара", "L/S", "Стейк", "Вход"]]
    for t in trades[:50]:
        bot = BOT_LABELS.get(str(t.get("bot")), str(t.get("bot") or "?"))[:3]
        side = "S" if t.get("is_short") else "L"
        pair = str(t.get("pair") or "?")
        if pair.endswith(":USDT"):
            pair = pair.replace("/USDT:USDT", "").replace(":USDT", "")
        rows.append(
            [
                str(t.get("id") or ""),
                bot,
                pair[:12],
                side,
                _fmt_money(t.get("stake_amount")),
                _fmt_rate(t.get("open_rate")),
            ]
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, r in enumerate(rows):
        line = "  ".join(r[i].ljust(widths[i]) for i in range(len(r)))
        lines.append(line)
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(r))))
    stake_sum = sum(float(t.get("stake_amount") or 0) for t in trades)
    lines.append("")
    lines.append(f"Всего: {len(trades)}  |  стоимость: {stake_sum:.2f} USDT")
    if len(trades) > 50:
        lines.append(f"… показаны первые 50 из {len(trades)}")
    table = "\n".join(lines)
    return f"Открытые сделки:\n<pre>{_escape_html(table)}</pre>"


def _trade_pnl_abs(t: dict) -> float:
    pnl = t.get("close_profit_abs")
    if pnl is None:
        pnl = t.get("realized_profit") or 0
    try:
        return float(pnl or 0)
    except (TypeError, ValueError):
        return 0.0


def _trade_pnl_fee_info(t: dict) -> dict:
    gross = _trade_pnl_abs(t)
    return tg.pnl_with_fees(
        gross,
        amount=t.get("amount"),
        open_rate=t.get("open_rate"),
        close_rate=t.get("close_rate"),
        stake_amount=t.get("stake_amount"),
        leverage=t.get("leverage"),
        profit_pct=(float(t.get("close_profit") or 0) * 100.0)
        if t.get("close_profit") is not None
        else None,
    )


def _format_profit_today(day_label: str, trades: list[dict]) -> str:
    """Daily summary: wins/losses by net (with fees), plus gross and fee totals."""
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    net_win = 0.0
    net_loss = 0.0
    fees_sum = 0.0
    for t in trades:
        info = _trade_pnl_fee_info(t)
        fees_sum += info["fees"]
        if info["net"] >= 0:
            wins += 1
            gross_win += info["gross"]
            net_win += info["net"]
        else:
            losses += 1
            gross_loss += info["gross"]
            net_loss += info["net"]
    gross_total = gross_win + gross_loss
    net_total = net_win + net_loss
    total = wins + losses

    rows = [
        ["", "шт", "без ком.", "с ком."],
        ["Хорошие", str(wins), f"+{gross_win:.2f}", f"+{net_win:.2f}"],
        ["Плохие", str(losses), f"{gross_loss:.2f}", f"{net_loss:.2f}"],
        ["Итого", str(total), f"{gross_total:+.2f}", f"{net_total:+.2f}"],
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = [f"Сводка за {day_label}", ""]
    for idx, r in enumerate(rows):
        line = "  ".join(r[i].ljust(widths[i]) for i in range(4))
        lines.append(line)
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(4)))
    lines.append("")
    lines.append(f"Комиссия ≈ {fees_sum:.2f} USDT (0.1%×2)")
    table = "\n".join(lines)
    if total == 0:
        return f"Сводка за {day_label}\nЗакрытых сделок сегодня нет."
    return f"<pre>{_escape_html(table)}</pre>"


def _require_linked(chat_id: int | str) -> str | None:
    linked = tg.user_for_chat(str(chat_id))
    if linked:
        return linked
    _send(
        chat_id,
        f"Аккаунт сайта не привязан.\n"
        f"Chat id: `{chat_id}`\n"
        "Откройте /start и сохраните id в Настройках на сайте.",
        markdown=True,
    )
    return None


def _handle_status(chat_id: int | str, *, table: bool = False) -> None:
    linked = _require_linked(chat_id)
    if not linked:
        return

    link = tg.get_link(linked)
    en = "да" if link.get("enabled") else "нет"
    trades = _collect_open_trades(linked)
    if table:
        body = _format_status_table(trades)
        text = (
            f"Статус: «{linked}» · уведомления: {en}\n"
            f"{body}"
        )
        _send(chat_id, text, html=True)
        return

    text = (
        f"Статус: привязан к «{linked}».\n"
        f"Уведомления: {en}.\n"
        f"Chat id: `{chat_id}`\n\n"
        f"{_format_status_trades(trades)}"
    )
    _send(chat_id, text, markdown=True)


def _handle_profit(chat_id: int | str) -> None:
    linked = _require_linked(chat_id)
    if not linked:
        return
    day_label, trades = _collect_closed_today(linked)
    body = _format_profit_today(day_label, trades)
    _send(chat_id, body, html=("<pre>" in body))


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
        # /status, /status@bot, /status table, /status_table
        rest = low.split(maxsplit=1)
        arg = rest[1] if len(rest) > 1 else ""
        # strip @botname from first token already handled by startswith path
        cmd0 = rest[0]
        if "table" in arg or cmd0.startswith("/status_table"):
            _handle_status(chat_id, table=True)
        else:
            _handle_status(chat_id, table=False)
        return
    if low.startswith("/profit") or low.startswith("/daily"):
        _handle_profit(chat_id)
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
