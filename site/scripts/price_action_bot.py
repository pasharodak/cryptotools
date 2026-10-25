#!/usr/bin/env python3
"""Standalone Price Action bot: Trend Pullback + Liquidity Sweep.

Scans public Bybit candles, scores the two setups, and publishes signals to
signal_bus for trade-executor. It has no exchange keys and never places orders.
Start/stop is the per-user ``price_action`` trading flag.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd

BASE = Path(os.environ.get("CT_BASE", Path(__file__).resolve().parents[1]))
SCRIPTS = BASE / "scripts"
for path in (BASE, BASE.parent, SCRIPTS):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pair_entry_guard  # noqa: E402
import price_action_settings as settings  # noqa: E402
import signal_bus  # noqa: E402
import user_trading  # noqa: E402
from simulation.strategies.PriceActionSignals import analyze_pair  # noqa: E402
from user_exchange import (  # noqa: E402
    fetch_public_klines,
    fetch_public_orderbook,
    fetch_public_ticker,
    ft_pair_to_symbol,
)

log = logging.getLogger("price_action")
INTERVAL_SEC = 60
REQUEST_PAUSE_SEC = 0.12
SCAN_WORKERS = 6
STATUS_PATH = BASE / "user_data" / "price_action_status.json"
DEPTH_BAND = 0.005
RECENT_SIGNALS: dict[str, float] = {}
_SIGNAL_LOCK = threading.Lock()
_FRAMES = (("5m", "5", 260, 5), ("15m", "15", 200, 15), ("1h", "60", 160, 60), ("1m", "1", 120, 1))


def _status(payload: dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_PATH)
    except OSError as exc:
        log.warning("status write failed: %s", exc)


def _frame(rows: list[list[Any]], interval_minutes: int) -> pd.DataFrame | None:
    if not rows:
        return None
    now_ms = int(time.time() * 1000)
    closed = [
        row
        for row in rows
        if len(row) >= 6 and int(row[0]) + interval_minutes * 60_000 <= now_ms
    ]
    if len(closed) < 20:
        return None
    df = pd.DataFrame(
        closed,
        columns=["date_ms", "open", "high", "low", "close", "volume", "turnover"][: len(closed[0])],
    )
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date_ms"] = pd.to_numeric(df["date_ms"], errors="coerce")
    df["dt"] = pd.to_datetime(df["date_ms"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df


def _depth_usdt(levels: list[list[str]], mid: float, band: float) -> float:
    if mid <= 0:
        return 0.0
    total = 0.0
    for row in levels or []:
        try:
            price = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and abs(price - mid) / mid <= band:
            total += price * size
    return total


def _metrics(symbol: str, ticker: dict[str, Any], book: dict[str, Any], fallback: float) -> dict[str, Any]:
    bid = _f(ticker.get("bid1Price"))
    ask = _f(ticker.get("ask1Price"))
    last = _f(ticker.get("lastPrice")) or fallback
    mid = (bid + ask) / 2.0 if bid and ask and bid > 0 and ask > 0 else last
    spread_pct = None
    if bid and ask and bid > 0 and ask > 0 and mid > 0:
        spread_pct = (ask - bid) / mid * 100.0
    bids = book.get("b") if isinstance(book.get("b"), list) else []
    asks = book.get("a") if isinstance(book.get("a"), list) else []
    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "bid_depth_usdt": _depth_usdt(bids, mid, DEPTH_BAND),
        "ask_depth_usdt": _depth_usdt(asks, mid, DEPTH_BAND),
        "turnover24h": _f(ticker.get("turnover24h")),
        "funding_rate": _f(ticker.get("fundingRate")),
    }


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _universe(traders: list[str], pairs_override: list[str] | None = None) -> list[str]:
    if pairs_override:
        return list(pairs_override)
    seen: set[str] = set()
    out: list[str] = []
    for user_id in traders:
        for pair in settings.pairs_for(user_id):
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    try:
        blocked = pair_entry_guard.pairs_blocked_for("price_action")
    except Exception as exc:
        log.warning("guard pairs lookup failed: %s", exc)
        blocked = set()
    free = [pair for pair in out if pair not in blocked]
    skipped = [pair for pair in out if pair in blocked]
    if skipped:
        log.info("skip pairs held by other bots: %s", ",".join(skipped))
    return free


def _dedupe_ok(pair: str, tag: str, side: str) -> bool:
    bucket = int(time.time()) // signal_bus.SIGNAL_DEDUPE_SEC
    key = f"{pair}|{tag}|{side}|{bucket}"
    now = time.time()
    with _SIGNAL_LOCK:
        stale = [
            k for k, ts in RECENT_SIGNALS.items() if now - ts > signal_bus.SIGNAL_DEDUPE_SEC * 2
        ]
        for k in stale:
            RECENT_SIGNALS.pop(k, None)
        if key in RECENT_SIGNALS:
            return False
        RECENT_SIGNALS[key] = now
        return True


def _publish(card: dict[str, Any], leverage: float) -> dict[str, Any] | None:
    entry = float(card.get("entry") or 0)
    sl = float(card.get("sl") or 0)
    tp = float(card.get("tp1") or 0)
    ratios = settings.trade_ratios(entry, sl, tp, leverage)
    if ratios is None:
        return None
    sl_ratio, tp_ratio = ratios
    pair = str(card["pair"])
    tag = str(card["tag"])
    side = str(card["side"])
    if not _dedupe_ok(pair, tag, side):
        return {"event": "deduped", "pair": pair, "tag": tag, "side": side}
    regime = card.get("regime") if isinstance(card.get("regime"), dict) else {}
    payload = {
        "event": "entry_signal",
        "pair": pair,
        "side": side,
        "strategy_id": tag,
        "entry_tag": tag,
        "rate": entry,
        "scenario": {
            "scenario_id": "price_action",
            "label": str(card.get("setup_label") or "Price Action"),
        },
        "indicators": {
            "pa_score": f"{int(card.get('score') or 0)}/8",
            "pa_regime": str(regime.get("label") or ""),
            "pa_setup": str(card.get("setup") or ""),
            "pa_rr_net": f"{float(card.get('rr_net') or 0):.2f}",
            "pa_spread": f"{float(card.get('spread_pct') or 0):.3f}%",
            "pa_depth": f"${float(card.get('depth_usdt') or 0):,.0f}",
        },
        "ml": {},
        "bot": "price_action",
        "score": int(card.get("score") or 0),
        "setup": str(card.get("setup") or ""),
        "regime": str(regime.get("key") or ""),
        "rr_net": float(card.get("rr_net") or 0),
        "risk_pct": float(card.get("risk_pct") or 0),
        "spread_pct": float(card.get("spread_pct") or 0),
        "depth_usdt": float(card.get("depth_usdt") or 0),
        "turnover24h": float(card.get("turnover24h") or 0),
        "sl_price": sl,
        "tp1_price": tp,
        "tp2_price": float(card.get("tp2") or 0),
        "fees_pct": float(card.get("fees_pct") or 0),
        "funding_pct": float(card.get("funding_pct") or 0),
        "leverage_reference": float(leverage),
        "stop_loss_ratio": sl_ratio,
        "take_profit_ratio": tp_ratio,
    }
    event = signal_bus.publish(payload)
    log.info(
        "SIGNAL %s %s %s score=%s rr=%.2f entry=%.8g sl=%.8g tp1=%.8g",
        side,
        pair,
        card.get("setup_label"),
        card.get("score"),
        float(card.get("rr_net") or 0),
        entry,
        sl,
        tp,
    )
    return event


def _scan_pair(
    pair: str,
    *,
    params: dict[str, Any],
    dry_run: bool,
    leverage: float,
    preview: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    symbol = ft_pair_to_symbol(pair)
    frames: dict[str, pd.DataFrame | None] = {}
    for name, interval, limit, minutes in _FRAMES:
        rows = fetch_public_klines(symbol, interval=interval, limit=limit)
        frames[name] = _frame(rows, minutes)
        time.sleep(REQUEST_PAUSE_SEC)
    fallback = 0.0
    df5 = frames.get("5m")
    if df5 is not None and not df5.empty:
        fallback = float(df5["close"].iloc[-1])
    ticker = fetch_public_ticker(symbol)
    time.sleep(REQUEST_PAUSE_SEC)
    book = fetch_public_orderbook(symbol, limit=50)
    metrics = _metrics(symbol, ticker, book, fallback)
    base_kwargs = settings.analyze_kwargs(params)
    cards: list[dict[str, Any]] = []
    for setup, enabled in (
        ("trend_pullback", bool(params["enable_trend_pullback"])),
        ("liquidity_sweep", bool(params["enable_liquidity_sweep"])),
    ):
        if not enabled:
            continue
        kwargs = dict(base_kwargs)
        kwargs["enable_trend_pullback"] = setup == "trend_pullback"
        kwargs["enable_liquidity_sweep"] = setup == "liquidity_sweep"
        cards.append(analyze_pair(pair, frames, metrics, **kwargs))
    if not cards:
        kwargs = dict(base_kwargs)
        kwargs["enable_trend_pullback"] = False
        kwargs["enable_liquidity_sweep"] = False
        cards.append(analyze_pair(pair, frames, metrics, **kwargs))
    for card in cards:
        card["bar_time"] = (
            df5["dt"].iloc[-1].isoformat() if df5 is not None and not df5.empty else None
        )
        card["symbol"] = symbol

    def rank(card: dict[str, Any]) -> tuple[int, int, float]:
        return (
            1 if card.get("decision") in ("LONG", "SHORT") else 0,
            int(card.get("score") or 0),
            float(card.get("rr_net") or 0.0),
        )

    cards.sort(key=rank, reverse=True)
    card = cards[0]
    if len(cards) > 1:
        card["alternatives"] = [
            {
                "setup": other.get("setup"),
                "setup_label": other.get("setup_label"),
                "decision": other.get("decision"),
                "side": other.get("side"),
                "score": other.get("score"),
                "reason": other.get("reason"),
            }
            for other in cards[1:]
        ]
    events: list[dict[str, Any]] = []
    for candidate in cards:
        if candidate.get("decision") not in ("LONG", "SHORT") or preview:
            continue
        if dry_run:
            log.info(
                "DRY %s %s setup=%s score=%s rr=%.2f",
                candidate.get("side"),
                pair,
                candidate.get("setup"),
                candidate.get("score"),
                float(candidate.get("rr_net") or 0),
            )
            candidate["dry_run"] = True
        else:
            event = _publish(candidate, leverage)
            if event:
                events.append(event)
    return card, events


def _tick(
    *,
    dry_run: bool = False,
    pairs_override: list[str] | None = None,
    preview: bool = False,
) -> None:
    traders = user_trading.list_active_traders("price_action")
    if not traders and not pairs_override:
        _status(
            {
                "state": "idle",
                "reason": "нет пользователей с включённым Price Action",
                "traders": [],
                "pairs": [],
                "cards": [],
                "signals": [],
            }
        )
        log.info("idle: no active price_action traders")
        return
    users_for_params = traders or ["preview"]
    params = settings.floor_params(users_for_params)
    if (
        not preview
        and not pairs_override
        and not params["enable_trend_pullback"]
        and not params["enable_liquidity_sweep"]
    ):
        _status(
            {
                "state": "idle",
                "reason": "у активных пользователей оба сетапа выключены",
                "traders": traders,
                "pairs": [],
                "cards": [],
                "signals": [],
            }
        )
        log.info("idle: both setups disabled")
        return
    pairs = _universe(traders, pairs_override)
    started = time.monotonic()
    if not pairs:
        _status(
            {
                "state": "idle",
                "reason": "нет свободных пар",
                "traders": traders,
                "pairs": [],
                "cards": [],
                "signals": [],
                "preview": preview,
            }
        )
        return
    cards: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    results: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    workers = max(1, min(SCAN_WORKERS, len(pairs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _scan_pair,
                pair,
                params=params,
                dry_run=dry_run,
                leverage=float(params["leverage"]),
                preview=preview,
            ): pair
            for pair in pairs
        }
        for future in as_completed(futures):
            pair = futures[future]
            try:
                results[pair] = future.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("scan %s failed: %s", pair, exc)
                errors.append({"pair": pair, "error": str(exc)[:200]})
    for pair in pairs:
        result = results.get(pair)
        if result is None:
            continue
        card, events = result
        cards.append(card)
        for event in events:
            if event.get("event") != "entry_signal":
                continue
            signals.append(
                {
                    "pair": pair,
                    "tag": event.get("strategy_id"),
                    "side": event.get("side"),
                    "setup": event.get("setup"),
                    "score": event.get("score"),
                    "rr_net": event.get("rr_net"),
                    "event_id": event.get("id"),
                    "seq": event.get("seq"),
                }
            )
    errors.sort(key=lambda row: row["pair"])
    elapsed = time.monotonic() - started
    _status(
        {
            "state": "ran" if not errors or cards else "error",
            "reason": "" if not errors else f"ошибки: {len(errors)}",
            "traders": traders,
            "pairs": pairs,
            "cards": cards,
            "signals": signals,
            "errors": errors,
            "params": {
                "enable_trend_pullback": params["enable_trend_pullback"],
                "enable_liquidity_sweep": params["enable_liquidity_sweep"],
                "min_score": params["min_score"],
                "min_rr": params["min_rr"],
                "max_spread_pct": params["max_spread_pct"],
                "min_depth_usdt": params["min_depth_usdt"],
                "max_risk_pct": params["max_risk_pct"],
            },
            "elapsed_sec": round(elapsed, 2),
            "preview": preview,
            "dry_run": dry_run,
        }
    )
    log.info(
        "cycle done pairs=%s workers=%s signals=%s errors=%s elapsed=%.1fs",
        len(pairs),
        workers,
        len(signals),
        len(errors),
        elapsed,
    )


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s UTC - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC - %(levelname)s - %(message)s")
    try:
        log_dir = BASE / "user_data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "price_action.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        log.warning("log file unavailable: %s", exc)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Price Action signal bot")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="do not publish signals")
    parser.add_argument(
        "--pairs",
        default="",
        help="comma-separated override (also works without active traders for preview)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="analyze override pairs without publishing (UI preview)",
    )
    args = parser.parse_args(argv)
    _setup_logging()
    pairs_override = settings.normalize_pairs(args.pairs) if args.pairs.strip() else None
    preview = bool(args.preview or (pairs_override and not user_trading.list_active_traders("price_action")))
    log.info(
        "Price Action bot started interval=%ss pairs=%s preview=%s dry_run=%s",
        INTERVAL_SEC,
        ",".join(pairs_override or []) or "per-user",
        preview,
        args.dry_run,
    )
    while True:
        started = time.monotonic()
        try:
            _tick(dry_run=args.dry_run, pairs_override=pairs_override, preview=preview)
        except Exception:
            log.exception("price action cycle failed")
            _status({"state": "error", "reason": "cycle failed", "cards": [], "signals": []})
        if args.once:
            break
        time.sleep(max(5.0, INTERVAL_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
