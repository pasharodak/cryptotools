#!/usr/bin/env python3
"""Standalone DeepSeek bot: ShareLLM signals on liquid USDT perps.

Does not run inside MultiStrategyRouter. One cycle every 3 minutes, and only
when at least one user has this bot started.
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
STRATS = BASE / "user_data" / "strategies"
for path in (BASE, BASE.parent, SCRIPTS, STRATS):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

import llm_book  # noqa: E402
import llm_settings  # noqa: E402
import pair_entry_guard  # noqa: E402
import signal_bus  # noqa: E402
import user_trading  # noqa: E402
from user_exchange import fetch_public_klines, ft_pair_to_symbol  # noqa: E402
from DeepseekLlmStrategy import DeepseekLlmStrategy, last_llm_confidence  # noqa: E402
from simulation.strategies.deepseek_openrouter import (  # noqa: E402
    LlmQuotaError,
    endpoint_ready,
    endpoint_url,
    model_name,
)

log = logging.getLogger("llm_bot")
book_log = logging.getLogger("llm50")
INTERVAL_SEC = 1800
QUOTA_PAUSE_SEC = 900
STATUS_PATH = BASE / "user_data" / "llm_bot_status.json"


def _status(payload: dict) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_PATH)
    except OSError as exc:
        log.warning("status write failed: %s", exc)


def _llm_up() -> bool:
    return endpoint_ready()


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
        strategy_id="DeepseekLlmStrategy",
        rate=price,
        entry_tag="DeepseekLlmStrategy",
        scenario={"scenario_id": "deepseek_llm", "label": "DeepSeek"},
        indicators={"llm_conf": f"{conf:.2f}"},
        ml={
            "predicted": "profit",
            "confidence_profit": conf,
            "confidence_loss": max(0.0, 1.0 - conf),
            "ready": True,
            "model_scope": f"sharellm:{model_name()}",
        },
        bot="llm",
    )
    log.info("published %s %s conf=%.2f event=%s", side, pair, conf, published.get("event"))


def _scan_pair(strategy: DeepseekLlmStrategy, pair: str) -> dict:
    frame = _frame(pair)
    if frame.empty:
        return {"pair": pair, "state": "error", "reason": "no candles"}
    meta = {"pair": pair}
    frame = strategy.populate_indicators(frame, meta)
    frame = strategy.populate_entry_trend(frame, meta)
    last = frame.iloc[-1]
    long_on = int(last.get("enter_long") or 0) == 1
    short_on = int(last.get("enter_short") or 0) == 1
    side = "long" if long_on else "short" if short_on else ""
    conf = last_llm_confidence(pair)
    price = float(last.get("close") or 0)
    if side and price > 0:
        _publish(pair, side, price, conf)
    else:
        log.info("DeepSeek HOLD %s conf=%.2f price=%s", pair, conf, price)
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
        for pair in llm_settings.pairs_for(user_id):
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    blocked = pair_entry_guard.pairs_blocked_for("llm")
    free = [pair for pair in out if pair not in blocked]
    skipped = [pair for pair in out if pair not in set(free)]
    if skipped:
        log.info("DeepSeek skip already open pairs: %s", ",".join(skipped))
    return free


def _tick(strategy: DeepseekLlmStrategy) -> bool:
    traders = user_trading.list_active_traders("llm")
    if not traders:
        _status({"state": "idle", "reason": "no traders", "traders": [], "pairs": []})
        log.info("no users with DeepSeek started — skip model call")
        return False
    universe = _universe(traders)
    if not _llm_up():
        _status({"state": "wait", "reason": "llm down", "traders": traders, "pairs": universe})
        log.warning("DeepSeek gateway is not responding: %s", endpoint_url())
        return False
    if not universe:
        _status({"state": "idle", "reason": "no pairs", "traders": traders, "pairs": []})
        log.info("DeepSeek users have an empty pair list — skip model call")
        return False
    # Strategy allowlist reads this env. Restore it so the next tick still
    # sees the process default for users who have not saved a list.
    previous = os.environ.get("CT_LLM_PAIRS")
    previous_conf = os.environ.get("CT_LLM_MIN_CONFIDENCE")
    os.environ["CT_LLM_PAIRS"] = ",".join(universe)
    # Publish when the least strict active user would take the signal.
    # trade-executor applies each user's own threshold after that.
    os.environ["CT_LLM_MIN_CONFIDENCE"] = f"{llm_settings.publish_floor(traders):.4f}"
    decisions: list[dict] = []
    quota = False
    try:
        for pair in universe:
            try:
                decisions.append(_scan_pair(strategy, pair))
            except LlmQuotaError as exc:
                quota = True
                log.warning("DeepSeek quota exhausted on %s — skip the other pairs", pair)
                decisions.append({"pair": pair, "state": "error", "reason": str(exc)[:160]})
                break
            except Exception as exc:
                log.exception("pair failed %s: %s", pair, exc)
                decisions.append({"pair": pair, "state": "error", "reason": str(exc)[:160]})
    finally:
        if previous is None:
            os.environ.pop("CT_LLM_PAIRS", None)
        else:
            os.environ["CT_LLM_PAIRS"] = previous
        if previous_conf is None:
            os.environ.pop("CT_LLM_MIN_CONFIDENCE", None)
        else:
            os.environ["CT_LLM_MIN_CONFIDENCE"] = previous_conf
    signals = [row for row in decisions if row.get("state") == "signal"]
    _status(
        {
            "state": "error" if quota else "signal" if signals else "flat",
            "reason": "insufficient_user_quota" if quota else "",
            "decisions": decisions,
            "signals": len(signals),
            "traders": traders,
            "pairs": universe,
        }
    )
    return quota


def _book_market() -> list[dict]:
    from simulation.strategies.DeepseekLlmStrategy import market_snapshot

    market: list[dict] = []
    for pair in llm_book.pairs():
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
            "strategy_id": llm_book.TAG,
            "entry_tag": llm_book.TAG,
            "rate": float(price),
            "scenario": {"scenario_id": "deepseek_book", "label": "DeepSeek 50"},
            "indicators": {"llm_conf": f"{float(action.get('confidence') or 0):.2f}"},
            "ml": {
                "predicted": "profit",
                "confidence_profit": float(action.get("confidence") or 0),
                "confidence_loss": max(0.0, 1.0 - float(action.get("confidence") or 0)),
                "ready": True,
                "model_scope": f"sharellm:{model_name()}",
            },
            "bot": llm_book.BOT,
            "stake_usdt": stake,
            "stop_loss_ratio": sl,
            "take_profit_ratio": tp,
        }
    )


def _book_tick() -> bool:
    from simulation.strategies.deepseek_openrouter import chat_completion

    traders = user_trading.list_active_traders(llm_book.BOT)
    if not traders:
        llm_book.write_status(BASE, {"state": "idle", "reason": "no traders", "users": {}})
        return False
    llm_book.write_status(BASE, {"state": "wait", "reason": "calling", "traders": traders, "users": {}})
    if not _llm_up():
        llm_book.write_status(
            BASE, {"state": "wait", "reason": "llm down", "traders": traders, "users": {}}
        )
        book_log.warning("DeepSeek 50: gateway is not responding: %s", endpoint_url())
        return False
    try:
        market = _book_market()
    except Exception as exc:
        book_log.exception("DeepSeek 50 market snapshot failed: %s", exc)
        llm_book.write_status(BASE, {"state": "error", "reason": str(exc)[:200], "users": {}})
        return False
    if not market:
        llm_book.write_status(BASE, {"state": "error", "reason": "no candles", "users": {}})
        return False
    book_log.info("DeepSeek 50 call model=%s url=%s pairs=%s", model_name(), endpoint_url(), len(market))
    users: dict = {}
    for user_id in traders:
        db_path = llm_book.trades_db_for(user_id)
        positions = llm_book.open_positions(db_path)
        free = llm_book.free_margin(positions)
        used = llm_book.used_margin(positions)
        own = {row["pair"] for row in positions}
        view = pair_entry_guard.market_for_entries(market, bot="llm50", own_pairs=own)
        if not view and not positions:
            book_log.info("DeepSeek 50 user=%s — all pairs already open, skip model call", user_id)
            users[user_id] = {
                "state": "ran",
                "reason": "все пары уже открыты другими ботами",
                "actions": [],
                "stats": llm_book.stats(db_path),
            }
            continue
        try:
            raw = chat_completion(
                llm_book.build_messages(view, positions, free=free, used=used),
                max_tokens=1500,
                timeout=150,
            )
            actions = llm_book.parse_actions(raw)
        except LlmQuotaError as exc:
            book_log.warning("DeepSeek 50 quota exhausted user=%s", user_id)
            users[user_id] = {"state": "error", "reason": str(exc)[:160], "actions": []}
            llm_book.write_status(BASE, {"state": "error", "reason": str(exc)[:160], "traders": traders, "users": users})
            return True
        except Exception as exc:
            book_log.warning("DeepSeek 50 call failed user=%s: %s", user_id, exc)
            users[user_id] = {"state": "error", "reason": str(exc)[:160], "actions": []}
            continue
        taken: list[dict] = []
        held_pairs = {row["pair"] for row in positions}
        cash = free
        for action in actions:
            pair = action["pair"]
            kind = action["action"]
            if kind in {"HOLD"}:
                taken.append({**action, "result": "hold"})
                continue
            if kind == "CLOSE":
                if pair not in held_pairs:
                    taken.append({**action, "result": "no_position"})
                    continue
                published = signal_bus.publish(
                    {
                        "event": "exit_signal",
                        "bot": llm_book.BOT,
                        "pair": pair,
                        "strategy_id": llm_book.TAG,
                        "user_id": user_id,
                    }
                )
                book_log.info("DeepSeek 50 CLOSE %s user=%s event=%s", pair, user_id, published.get("event"))
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
            levels = llm_book.ratios_for(action, price, side)
            stake = llm_book.stake_for(action, cash)
            if price <= 0 or levels is None or stake < llm_book.MIN_STAKE:
                taken.append({**action, "result": "skip"})
                book_log.info(
                    "DeepSeek 50 skip %s %s user=%s price=%s stake=%s",
                    kind,
                    pair,
                    user_id,
                    price,
                    stake,
                )
                continue
            sl, tp = levels
            published = _book_emit(user_id, action, price, stake, sl, tp)
            cash = round(max(0.0, cash - stake), 2)
            held_pairs.add(pair)
            book_log.info(
                "DeepSeek 50 %s %s user=%s stake=%.2f conf=%.2f sl=%.4f tp=%.4f",
                kind,
                pair,
                user_id,
                stake,
                float(action.get("confidence") or 0),
                sl,
                tp,
            )
            taken.append({**action, "result": "signal", "stake_usdt": stake, "event": published.get("event")})
        users[user_id] = {
            "state": "ran",
            "free_usdt": free,
            "used_usdt": used,
            "actions": taken,
            "stats": llm_book.stats(db_path),
        }
    llm_book.write_status(BASE, {"state": "ran", "traders": traders, "users": users})
    return False


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
        file_handler = logging.FileHandler(log_dir / "deepseek.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(lambda record: not record.name.startswith("llm50"))
        logging.getLogger().addHandler(file_handler)
        book_handler = logging.FileHandler(log_dir / "deepseek50.log", encoding="utf-8")
        book_handler.setFormatter(fmt)
        book_log.setLevel(logging.INFO)
        book_log.addHandler(book_handler)
        book_log.propagate = True
    except OSError as exc:
        log.warning("deepseek log file unavailable: %s", exc)
    strategy = DeepseekLlmStrategy({})
    log.info(
        "DeepSeek bot started model=%s url=%s pairs=%s interval=%ss",
        model_name(),
        endpoint_url(),
        ",".join(llm_settings.pairs()),
        INTERVAL_SEC,
    )
    book_log.info(
        "DeepSeek 50 ready budget=%s USDT leverage=%s pairs=%s — waiting for Start",
        llm_book.BUDGET_USDT,
        llm_book.LEVERAGE,
        len(llm_book.pairs()),
    )
    while True:
        started = time.monotonic()
        # DeepSeek 50 is one gateway call. Run it before the slower per-pair scan
        # so a stale OpenSeek error does not sit on the card for the whole cycle.
        quota = False
        try:
            quota = bool(_book_tick())
        except Exception as exc:
            book_log.exception("DeepSeek 50 cycle failed: %s", exc)
        try:
            quota = bool(_tick(strategy)) or quota
        except Exception as exc:
            log.exception("cycle failed: %s", exc)
            _status({"state": "error", "reason": str(exc)[:200]})
        pause = QUOTA_PAUSE_SEC if quota else INTERVAL_SEC
        if quota:
            log.warning("free quota exhausted — next DeepSeek cycle in %ss", int(pause))
        time.sleep(max(5.0, pause - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
