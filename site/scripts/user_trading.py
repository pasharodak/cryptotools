#!/usr/bin/env python3
"""Per-user trading enable flags (strategy/grid) for shared-bot architecture."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import tenant_manager as tm

DEFAULT_FLAGS = {
    "strategy": False,
    "grid": False,
    "finder": False,
    "llm": False,
    "llm50": False,
    "cursor": False,
    "cursor50": False,
    "price_action": False,
}

RETIRED_BOTS = frozenset({"llm", "llm50", "cursor", "cursor50", "price_action"})

_ACTIVE_CACHE_LOCK = threading.Lock()
_ACTIVE_CACHE: dict[str, Any] = {}
_ACTIVE_CACHE_TTL_SEC = 15.0


def invalidate_active_traders_cache() -> None:
    with _ACTIVE_CACHE_LOCK:
        _ACTIVE_CACHE.clear()


def _path(user_id: str) -> Path:
    if user_id == "admin":
        return tm.BASE / "user_data" / "trading_enabled.json"
    return tm.tenant_user_data(user_id) / "trading_enabled.json"


def load_trading_flags(user_id: str) -> dict[str, bool]:
    path = _path(user_id)
    out = dict(DEFAULT_FLAGS)
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in DEFAULT_FLAGS:
        if key in data:
            out[key] = bool(data[key])
    for bot in RETIRED_BOTS:
        out[bot] = False
    return out


def save_trading_flags(user_id: str, flags: dict[str, Any]) -> dict[str, bool]:
    path = _path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_trading_flags(user_id)
    for key in DEFAULT_FLAGS:
        if key in flags:
            current[key] = bool(flags[key])
    for bot in RETIRED_BOTS:
        current[bot] = False
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # Keep control-plane desired in sync when flags change outside PATCH /bots
    try:
        import control_plane as cp

        cp.ensure_init()
        for key in DEFAULT_FLAGS:
            if key not in flags or key not in cp.BOTS:
                continue
            existing = cp.get_desired(user_id, key)
            want = bool(flags[key])
            if existing is None or bool(existing) != want:
                cp.set_desired(user_id, key, want, updated_by="trading_flags", enqueue=False)
    except Exception:
        pass
    return current


def is_trading_enabled(user_id: str, bot: str = "strategy") -> bool:
    if bot in RETIRED_BOTS:
        return False
    return bool(load_trading_flags(user_id).get(bot, False))


def exchanges_for_trade_on(trade_on: str) -> list[str]:
    """Expand ``all|bybit|binance`` to concrete exchange ids."""
    ton = tm.normalize_trade_on(trade_on)
    if ton == "all":
        return list(tm.SUPPORTED_EXCHANGES)
    return [tm.normalize_exchange(ton)]


def list_active_traders(bot: str = "strategy") -> list[str]:
    """Trading context ids with secrets + trading flag for the given bot.

    Union of ``trade_on_main`` and ``trade_on_test`` exchanges (entry gate
    filters per panel later). Flags are shared per live/demo mode on the
    Bybit context; exchange contexts inherit them.
    Legacy ``*_demo`` login rows are skipped.
    """
    if bot in RETIRED_BOTS:
        return []
    now = time.monotonic()
    with _ACTIVE_CACHE_LOCK:
        hit = _ACTIVE_CACHE.get(bot)
        if hit and (now - float(hit["ts"])) < _ACTIVE_CACHE_TTL_SEC:
            return list(hit["ids"])
    out: list[str] = []
    try:
        import user_prefs as up
    except Exception:
        up = None  # type: ignore
    for user in tm.load_users():
        uid = str(user.get("id") or "")
        if not uid or not user.get("enabled", True):
            continue
        if tm.is_demo_account(user):
            continue
        exchanges: list[str] = []
        if up is not None:
            try:
                panels = up.load_trade_on_panels(uid)
                seen: set[str] = set()
                for ton in (panels.get("trade_on_main"), panels.get("trade_on_test")):
                    for ex in exchanges_for_trade_on(str(ton or tm.DEFAULT_TRADE_ON)):
                        if ex not in seen:
                            seen.add(ex)
                            exchanges.append(ex)
            except Exception:
                exchanges = exchanges_for_trade_on(tm.DEFAULT_TRADE_ON)
        else:
            exchanges = exchanges_for_trade_on(tm.DEFAULT_TRADE_ON)
        live_flag = is_trading_enabled(tm.live_context_id(uid, exchange="bybit"), bot)
        demo_flag_cid = tm.demo_context_id(uid, exchange="bybit")
        demo_flag = is_trading_enabled(demo_flag_cid, bot)
        for ex in exchanges:
            if live_flag and tm.has_mode_secrets(uid, "live", exchange=ex):
                out.append(tm.live_context_id(uid, exchange=ex))
            if demo_flag and tm.has_mode_secrets(uid, "demo", exchange=ex):
                out.append(tm.demo_context_id(uid, exchange=ex))
    with _ACTIVE_CACHE_LOCK:
        _ACTIVE_CACHE[bot] = {"ids": list(out), "ts": now}
    return out
