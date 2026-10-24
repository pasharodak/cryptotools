#!/usr/bin/env python3
"""Standalone Cursor bots: fixed SL/TP, and a shared 50 USDT book.

Same cycle shape as the DeepSeek bots. Calls happen only after a user
presses Start. The model is a Cursor cloud agent (no repository).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE = Path(os.environ.get("CT_BASE", Path(__file__).resolve().parents[1]))
SCRIPTS = BASE / "scripts"
for path in (BASE, BASE.parent, SCRIPTS):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

import cursor_book  # noqa: E402
import cursor_client  # noqa: E402
import cursor_settings  # noqa: E402
import pair_entry_guard  # noqa: E402
import signal_bus  # noqa: E402
import user_trading  # noqa: E402
from user_exchange import fetch_public_klines, ft_pair_to_symbol  # noqa: E402
from simulation.strategies.DeepseekLlmStrategy import market_snapshot  # noqa: E402
from simulation.strategies.deepseek_openrouter import (  # noqa: E402
    entry_sides,
    parse_prediction,
)

log = logging.getLogger("cursor_bot")
book_log = logging.getLogger("cursor50")
INTERVAL_SEC = 1800
RATE_PAUSE_SEC = 1800
STATUS_PATH = BASE / "user_data" / "cursor_bot_status.json"
HEALTH_PATH = BASE / "user_data" / "cursor_health.json"
SYSTEM_PROMPT = (
    "You are a Bybit USDT-perpetual trader. "
    "Stop loss and take profit prices in the JSON are suggestions only; "
    "the bot applies its own fixed stop and take-profit. "
    "Answer with one JSON object and nothing else."
)


def _status(payload: dict) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_PATH)
    except OSError as exc:
        log.warning("status write failed: %s", exc)


def _frame(pair: str) -> pd.DataFrame:
    symbol = ft_pair_to_symbol(pair)
    rows = fetch_public_klines(symbol, interval="5", limit=450)
    df = pd.DataFrame(rows, columns=["date_ms", "open", "high", "low", "close", "volume", "turnover"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date_ms"].astype("int64"), unit="ms", utc=True)
    df.dropna(subset=["close"], inplace=True)
    return df


def _publish(pair: str, side: str, price: float, conf: float) -> None:
    published = signal_bus.publish_entry_signal(
        pair=pair,
        side=side,
        strategy_id=cursor_settings.TAG,
        rate=price,
        entry_tag=cursor_settings.TAG,
        scenario={"scenario_id": "cursor_llm", "label": "Cursor"},
        indicators={"llm_conf": f"{conf:.2f}"},
        ml={
            "predicted": "profit",
            "confidence_profit": conf,
            "confidence_loss": max(0.0, 1.0 - conf),
            "ready": True,
            "model_scope": f"cursor:{cursor_client.model_name()}",
        },
        bot="cursor",
    )
    log.info("published %s %s conf=%.2f event=%s", side, pair, conf, published.get("event"))


def _pair_snapshot(pair: str) -> dict | None:
    frame = _frame(pair)
    if frame.empty:
        return None
    snap = market_snapshot(frame)
    return {
        "pair": pair,
        "price": snap.get("price"),
        "candles_5m": list(snap.get("candles_5m") or [])[-6:],
        "indicators_5m": snap.get("indicators_5m") or {},
        "indicators_1h": snap.get("indicators_1h") or {},
    }


def _batch_prompt(market: list[dict]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Decide every pair below in one answer. "
                "Return JSON {\"decisions\":[{\"pair\":\"BTC/USDT:USDT\",\"action\":\"HOLD\","
                "\"confidence\":0.0,\"reasoning\":\"short\"}]}. "
                "action is BUY, SELL, or HOLD. One item per pair, no extra pairs.\n"
                + json.dumps({"market": market}, ensure_ascii=False)
            ),
        },
    ]


def _parse_batch(raw: str, market: list[dict]) -> dict[str, dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    data = json.loads(text[start : end + 1] if start >= 0 and end > start else text)
    rows = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = data.get("actions") if isinstance(data, dict) else None
    found: dict[str, dict] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("pair"):
                found[str(row["pair"])] = parse_prediction(json.dumps(row))
    elif isinstance(data, dict) and data.get("action"):
        only = market[0]["pair"] if len(market) == 1 else ""
        if only:
            found[only] = parse_prediction(json.dumps(data))
    return found


def _apply_decision(pair: str, decision: dict, price: float, floor: float) -> dict:
    long_on, short_on = entry_sides(decision, min_conf=floor)
    side = "long" if long_on else "short" if short_on else ""
    try:
        conf = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if side and price > 0:
        _publish(pair, side, price, conf)
    else:
        log.info("Cursor HOLD %s conf=%.2f price=%s", pair, conf, price)
    return {
        "pair": pair,
        "state": "signal" if side else "flat",
        "side": side or "hold",
        "confidence": conf,
        "price": price,
    }


def _universe(traders: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for user_id in traders:
        for pair in cursor_settings.pairs_for(user_id):
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    blocked = pair_entry_guard.pairs_blocked_for("cursor")
    free = [pair for pair in out if pair not in blocked]
    skipped = [pair for pair in out if pair not in set(free)]
    if skipped:
        log.info("Cursor skip already open pairs: %s", ",".join(skipped))
    return free


def _tick() -> bool:
    traders = user_trading.list_active_traders("cursor")
    if not traders:
        _status({"state": "idle", "reason": "no traders", "traders": [], "pairs": []})
        log.info("no users with Cursor started — skip model call")
        return False
    universe = _universe(traders)
    if not cursor_client.configured():
        _status({"state": "wait", "reason": "cursor token missing", "traders": traders, "pairs": universe})
        log.warning("Cursor token is not set")
        return False
    if not universe:
        _status({"state": "idle", "reason": "no pairs", "traders": traders, "pairs": []})
        log.info("Cursor users have an empty pair list — skip model call")
        return False
    floor = cursor_settings.publish_floor(traders)
    market = [row for pair in universe if (row := _pair_snapshot(pair))]
    if not market:
        _status({"state": "error", "reason": "no candles", "traders": traders, "pairs": universe})
        return False
    decisions: list[dict] = []
    paused = False
    try:
        log.info("Cursor call model=%s pairs=%s", cursor_client.model_name(), len(market))
        raw = cursor_client.complete_messages(
            _batch_prompt(market),
            timeout=cursor_client.BOOK_TIMEOUT_SEC,
        )
        parsed = _parse_batch(raw, market)
        for row in market:
            pair = str(row["pair"])
            decision = parsed.get(pair) or {"action": "HOLD", "confidence": 0.0}
            decisions.append(_apply_decision(pair, decision, float(row.get("price") or 0), floor))
    except cursor_client.CursorRateError as exc:
        paused = True
        log.warning("Cursor rate limit — pause %.0fs", exc.retry_after)
        decisions.append({"pair": "", "state": "error", "reason": str(exc)[:160]})
    except Exception as exc:
        log.exception("Cursor scan failed: %s", exc)
        decisions.append({"pair": "", "state": "error", "reason": str(exc)[:160]})
    signals = [row for row in decisions if row.get("state") == "signal"]
    failed = next((row for row in decisions if row.get("state") == "error"), None)
    if paused:
        state, reason = "error", "лимит запросов Cursor, следующий цикл через 30 мин"
    elif failed and not signals:
        state, reason = "error", str(failed.get("reason") or "ошибка цикла")
    else:
        state, reason = ("signal" if signals else "flat"), ""
    _status(
        {
            "state": state,
            "reason": reason,
            "decisions": decisions,
            "signals": len(signals),
            "traders": traders,
            "pairs": universe,
            "model": cursor_client.model_name(),
        }
    )
    return paused


def _book_market() -> list[dict]:
    market: list[dict] = []
    for pair in cursor_book.pairs():
        frame = _frame(pair)
        if frame.empty:
            continue
        snap = market_snapshot(frame)
        candles = list(snap.get("candles_5m") or [])[-6:]
        market.append(
            {
                "pair": pair,
                "price": snap.get("price"),
                "candles_5m": candles,
                "indicators_5m": snap.get("indicators_5m") or {},
                "indicators_1h": snap.get("indicators_1h") or {},
            }
        )
    return market


def _book_emit(user_id: str, action: dict, price: float, stake: float, sl: float, tp: float) -> dict:
    side = "short" if action["action"] == "SELL" else "long"
    return signal_bus.publish(
        {
            "event": "entry_signal",
            "user_id": user_id,
            "pair": action["pair"],
            "side": side,
            "strategy_id": cursor_book.TAG,
            "entry_tag": cursor_book.TAG,
            "rate": float(price),
            "scenario": {"scenario_id": "cursor_book", "label": "Cursor 50"},
            "indicators": {"llm_conf": f"{float(action.get('confidence') or 0):.2f}"},
            "ml": {
                "predicted": "profit",
                "confidence_profit": float(action.get("confidence") or 0),
                "confidence_loss": max(0.0, 1.0 - float(action.get("confidence") or 0)),
                "ready": True,
                "model_scope": f"cursor:{cursor_client.model_name()}",
            },
            "bot": cursor_book.BOT,
            "stake_usdt": stake,
            "stop_loss_ratio": sl,
            "take_profit_ratio": tp,
        }
    )


def _book_tick() -> bool:
    traders = user_trading.list_active_traders(cursor_book.BOT)
    if not traders:
        cursor_book.write_status(BASE, {"state": "idle", "reason": "no traders", "users": {}})
        return False
    cursor_book.write_status(BASE, {"state": "wait", "reason": "calling", "traders": traders, "users": {}})
    if not cursor_client.configured():
        cursor_book.write_status(
            BASE, {"state": "wait", "reason": "cursor token missing", "traders": traders, "users": {}}
        )
        book_log.warning("Cursor 50: token is not set")
        return False
    try:
        market = _book_market()
    except Exception as exc:
        book_log.exception("Cursor 50 market snapshot failed: %s", exc)
        cursor_book.write_status(BASE, {"state": "error", "reason": str(exc)[:200], "users": {}})
        return False
    if not market:
        cursor_book.write_status(BASE, {"state": "error", "reason": "no candles", "users": {}})
        return False
    book_log.info("Cursor 50 call model=%s pairs=%s", cursor_client.model_name(), len(market))
    users: dict = {}
    for user_id in traders:
        db_path = cursor_book.trades_db_for(user_id)
        positions = cursor_book.open_positions(db_path)
        free = cursor_book.free_margin(positions)
        used = cursor_book.used_margin(positions)
        own = {row["pair"] for row in positions}
        view = pair_entry_guard.market_for_entries(market, bot="cursor50", own_pairs=own)
        if not view and not positions:
            book_log.info("Cursor 50 user=%s — all pairs already open, skip model call", user_id)
            users[user_id] = {
                "state": "ran",
                "reason": "все пары уже открыты другими ботами",
                "actions": [],
                "stats": cursor_book.stats(db_path),
            }
            continue
        try:
            raw = cursor_client.complete_messages(
                cursor_book.build_messages(view, positions, free=free, used=used),
                timeout=cursor_client.BOOK_TIMEOUT_SEC,
            )
            actions = cursor_book.parse_actions(raw)
        except cursor_client.CursorRateError as exc:
            book_log.warning("Cursor 50 rate limit user=%s — pause %.0fs", user_id, exc.retry_after)
            reason = "лимит запросов Cursor, следующий цикл через 30 мин"
            users[user_id] = {"state": "error", "reason": reason, "actions": []}
            cursor_book.write_status(BASE, {"state": "error", "reason": reason, "users": users})
            return True
        except Exception as exc:
            book_log.exception("Cursor 50 user=%s: %s", user_id, exc)
            users[user_id] = {"state": "error", "reason": str(exc)[:160], "actions": []}
            continue
        taken: list[dict] = []
        held_pairs = {row["pair"] for row in positions}
        cash = free
        for action in actions:
            pair = action["pair"]
            kind = action["action"]
            if kind == "HOLD":
                taken.append({**action, "result": "hold"})
                continue
            if kind == "CLOSE":
                if pair not in held_pairs:
                    taken.append({**action, "result": "no_position"})
                    continue
                published = signal_bus.publish(
                    {
                        "event": "exit_signal",
                        "bot": cursor_book.BOT,
                        "pair": pair,
                        "strategy_id": cursor_book.TAG,
                        "user_id": user_id,
                    }
                )
                book_log.info("Cursor 50 CLOSE %s user=%s event=%s", pair, user_id, published.get("event"))
                taken.append({**action, "result": "close"})
                continue
            if pair in held_pairs:
                taken.append({**action, "result": "already_open"})
                continue
            price = 0.0
            for row in market:
                if row["pair"] == pair:
                    price = float(row.get("price") or 0)
                    break
            side = "short" if kind == "SELL" else "long"
            levels = cursor_book.ratios_for(action, price, side)
            stake = cursor_book.stake_for(action, cash)
            if price <= 0 or levels is None or stake < cursor_book.MIN_STAKE:
                taken.append({**action, "result": "skip"})
                continue
            sl, tp = levels
            published = _book_emit(user_id, action, price, stake, sl, tp)
            cash = round(max(0.0, cash - stake), 2)
            held_pairs.add(pair)
            book_log.info(
                "Cursor 50 %s %s user=%s stake=%.2f sl=%.4f tp=%.4f",
                kind,
                pair,
                user_id,
                stake,
                sl,
                tp,
            )
            taken.append({**action, "result": "signal", "stake_usdt": stake, "event": published.get("event")})
        users[user_id] = {
            "state": "ran",
            "free_usdt": cash,
            "used_usdt": used,
            "actions": taken,
            "stats": cursor_book.stats(db_path),
        }
    cursor_book.write_status(BASE, {"state": "ran", "traders": traders, "users": users})
    return False


def _write_health(ok: bool, reason: str = "") -> None:
    payload = {
        "ok": ok,
        "reason": reason[:200],
        "model": cursor_client.model_name(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("health write failed: %s", exc)


def _probe() -> None:
    if not cursor_client.configured():
        _write_health(False, "cursor token missing")
        log.warning("Cursor token is not set")
        return
    try:
        text = cursor_client.complete('Reply with exactly {"ok":true}', timeout=90)
        ok = "ok" in text.lower()
        _write_health(ok, "" if ok else text[:160])
        log.info("Cursor probe ok=%s", ok)
    except Exception as exc:
        _write_health(False, str(exc)[:200])
        log.warning("Cursor probe failed: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s UTC - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        log_dir = BASE / "user_data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter(
            "%(asctime)s UTC - %(levelname)s - %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(log_dir / "cursor.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(lambda record: not record.name.startswith("cursor50"))
        logging.getLogger().addHandler(file_handler)
        book_handler = logging.FileHandler(log_dir / "cursor50.log", encoding="utf-8")
        book_handler.setFormatter(fmt)
        book_log.setLevel(logging.INFO)
        book_log.addHandler(book_handler)
        book_log.propagate = True
    except OSError as exc:
        log.warning("cursor log file unavailable: %s", exc)
    log.info(
        "Cursor bot started model=%s pairs=%s interval=%ss",
        cursor_client.model_name(),
        ",".join(cursor_settings.pairs()),
        INTERVAL_SEC,
    )
    book_log.info(
        "Cursor 50 ready budget=%s USDT leverage=%s pairs=%s — waiting for Start",
        cursor_book.BUDGET_USDT,
        cursor_book.LEVERAGE,
        len(cursor_book.pairs()),
    )
    _probe()
    while True:
        started = time.monotonic()
        pause = False
        try:
            pause = _book_tick() or _tick()
        except Exception:
            log.exception("cursor cycle failed")
        elapsed = time.monotonic() - started
        sleep_for = RATE_PAUSE_SEC if pause else max(5.0, INTERVAL_SEC - elapsed)
        if pause:
            log.warning("Cursor rate limit — next cycle in %ss", int(sleep_for))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
