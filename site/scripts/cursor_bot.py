#!/usr/bin/env python3
"""Cursor 50 book bot driven by vibe-trade DebateOrchestrator signals.

Fixed-SL Cursor bot is retired. This process only runs the $50 margin book:
vibe-trade consensus → BUY/SELL/CLOSE → signal_bus → trade-executor.
"""
from __future__ import annotations

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
import pair_entry_guard  # noqa: E402
import signal_bus  # noqa: E402
import user_trading  # noqa: E402
import vibe_trade_signals as vibe  # noqa: E402
from user_exchange import fetch_public_klines, ft_pair_to_symbol  # noqa: E402
from simulation.strategies.DeepseekLlmStrategy import market_snapshot  # noqa: E402

log = logging.getLogger("cursor_bot")
book_log = logging.getLogger("cursor50")
INTERVAL_SEC = int(os.environ.get("CT_CURSOR50_INTERVAL_SEC", "1800") or "1800")
HEALTH_PATH = BASE / "user_data" / "cursor_health.json"
ROTATION_PATH = BASE / "user_data" / "cursor50_vibe_rotation.txt"
_FRAME_CACHE: dict[str, pd.DataFrame] = {}


def _frame(pair: str) -> pd.DataFrame:
    cached = _FRAME_CACHE.get(pair)
    if cached is not None and not cached.empty:
        return cached
    symbol = ft_pair_to_symbol(pair)
    rows = fetch_public_klines(symbol, interval="5", limit=450)
    df = pd.DataFrame(rows, columns=["date_ms", "open", "high", "low", "close", "volume", "turnover"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date_ms"].astype("int64"), unit="ms", utc=True)
    df.dropna(subset=["close"], inplace=True)
    _FRAME_CACHE[pair] = df
    return df


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
                "model_scope": f"vibe-trade:{vibe.provider_label()}",
            },
            "bot": cursor_book.BOT,
            "stake_usdt": stake,
            "stop_loss_ratio": sl,
            "take_profit_ratio": tp,
        }
    )


def _collect_actions(
    *,
    market: list[dict],
    positions: list[dict],
    free: float,
    candidates: list[str],
) -> list[dict]:
    by_pair = {str(row.get("pair") or ""): row for row in market}
    pos_by_pair = {str(row.get("pair") or ""): row for row in positions}
    actions: list[dict] = []
    for pair in candidates:
        row = by_pair.get(pair)
        if not row:
            actions.append(
                {
                    "pair": pair,
                    "action": "HOLD",
                    "stake_usdt": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "confidence": 0.0,
                    "reasoning": "no market snapshot",
                    "result": "skip",
                }
            )
            continue
        try:
            bars = vibe.bars_from_frame(_frame(pair))
            action = vibe.debate_action(
                pair,
                bars,
                position=pos_by_pair.get(pair),
                free_margin=free,
            )
            actions.append(action)
        except Exception as exc:  # noqa: BLE001
            book_log.warning("vibe-trade skip %s: %s", pair, exc)
            actions.append(
                {
                    "pair": pair,
                    "action": "HOLD",
                    "stake_usdt": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "confidence": 0.0,
                    "reasoning": str(exc)[:200],
                    "result": "error",
                }
            )
    return actions


def _book_tick() -> bool:
    global _FRAME_CACHE
    _FRAME_CACHE = {}
    traders = user_trading.list_active_traders(cursor_book.BOT)
    if not traders:
        cursor_book.write_status(BASE, {"state": "idle", "reason": "no traders", "users": {}})
        return False
    cursor_book.write_status(BASE, {"state": "wait", "reason": "calling vibe-trade", "traders": traders, "users": {}})
    if not vibe.configured():
        reason = vibe.config_reason() or "vibe-trade LLM not configured"
        cursor_book.write_status(
            BASE, {"state": "wait", "reason": reason, "traders": traders, "users": {}}
        )
        book_log.warning("Cursor 50: %s", reason)
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
    book_log.info(
        "Cursor 50 vibe-trade provider=%s market_pairs=%s",
        vibe.provider_label(),
        len(market),
    )
    users: dict = {}
    for user_id in traders:
        db_path = cursor_book.trades_db_for(user_id)
        positions = cursor_book.open_positions(db_path)
        free = cursor_book.free_margin(positions)
        used = cursor_book.used_margin(positions)
        own = {row["pair"] for row in positions}
        view = pair_entry_guard.market_for_entries(market, bot="cursor50", own_pairs=own)
        view_pairs = [str(row.get("pair") or "") for row in view if row.get("pair")]
        candidates = vibe.pick_candidates(
            view_pairs or [str(r.get("pair") or "") for r in market],
            own,
            rotation_path=ROTATION_PATH,
            max_new=vibe.MAX_NEW_PAIRS,
        )
        # Always include open positions even if blocked in market view.
        for pair in sorted(own):
            if pair not in candidates:
                candidates.insert(0, pair)
        if not candidates:
            book_log.info("Cursor 50 user=%s — no candidate pairs", user_id)
            users[user_id] = {
                "state": "ran",
                "reason": "нет пар для debate",
                "actions": [],
                "stats": cursor_book.stats(db_path),
            }
            continue
        try:
            actions = _collect_actions(
                market=market,
                positions=positions,
                free=free,
                candidates=candidates,
            )
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
            if action.get("result") == "error":
                taken.append(action)
                continue
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
                "Cursor 50 %s %s user=%s stake=%.2f sl=%.4f tp=%.4f conf=%.2f",
                kind,
                pair,
                user_id,
                stake,
                sl,
                tp,
                float(action.get("confidence") or 0),
            )
            taken.append({**action, "result": "signal", "stake_usdt": stake, "event": published.get("event")})
        users[user_id] = {
            "state": "ran",
            "free_usdt": cash,
            "used_usdt": used,
            "actions": taken,
            "stats": cursor_book.stats(db_path),
            "provider": vibe.provider_label(),
        }
    cursor_book.write_status(BASE, {"state": "ran", "traders": traders, "users": users})
    return False


def _write_health(ok: bool, reason: str = "") -> None:
    payload = {
        "ok": ok,
        "reason": reason[:200],
        "model": vibe.provider_label(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_PATH.write_text(
            __import__("json").dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("health write failed: %s", exc)


def _probe() -> None:
    if not vibe.configured():
        reason = vibe.config_reason() or "vibe-trade LLM not configured"
        _write_health(False, reason)
        log.warning("Cursor 50 probe: %s", reason)
        return
    _write_health(True, "")
    log.info("Cursor 50 probe ok provider=%s", vibe.provider_label())


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
    book_log.info(
        "Cursor 50 vibe-trade ready budget=%s USDT leverage=%s pairs=%s interval=%ss",
        cursor_book.BUDGET_USDT,
        cursor_book.LEVERAGE,
        len(cursor_book.pairs()),
        INTERVAL_SEC,
    )
    _probe()
    while True:
        started = time.monotonic()
        try:
            _book_tick()
        except Exception:
            log.exception("cursor50 cycle failed")
        elapsed = time.monotonic() - started
        sleep_for = max(5.0, INTERVAL_SEC - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
