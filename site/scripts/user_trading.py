#!/usr/bin/env python3
"""Per-user trading enable flags (strategy/grid) for shared-bot architecture."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import tenant_manager as tm

DEFAULT_FLAGS = {"strategy": True, "grid": False, "finder": False}


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
    return out


def save_trading_flags(user_id: str, flags: dict[str, Any]) -> dict[str, bool]:
    path = _path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_trading_flags(user_id)
    for key in DEFAULT_FLAGS:
        if key in flags:
            current[key] = bool(flags[key])
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
            if key not in flags:
                continue
            existing = cp.get_desired(user_id, key)
            want = bool(flags[key])
            if existing is None or bool(existing) != want:
                cp.set_desired(user_id, key, want, updated_by="trading_flags", enqueue=False)
    except Exception:
        pass
    return current


def is_trading_enabled(user_id: str, bot: str = "strategy") -> bool:
    return bool(load_trading_flags(user_id).get(bot, False))


def list_active_traders(bot: str = "strategy") -> list[str]:
    """Users with secrets + trading flag for the given bot."""
    out: list[str] = []
    for user in tm.load_users():
        uid = str(user.get("id") or "")
        if not uid or not user.get("enabled", True):
            continue
        if uid == "admin":
            st = tm.admin_secrets_status()
        else:
            st = tm.secrets_status(uid)
        if not st.get("has_secrets"):
            continue
        if is_trading_enabled(uid, bot):
            out.append(uid)
    return out
