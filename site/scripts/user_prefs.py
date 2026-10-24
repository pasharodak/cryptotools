#!/usr/bin/env python3
"""Per-user trading prefs in control_plane.sqlite (source of truth).

Migrates from legacy JSON under user_data/ or tenants/{id}/ on first read.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import control_plane as cp
import tenant_manager as tm

log = logging.getLogger("user_prefs")

_UNION_CACHE_LOCK = threading.Lock()
_UNION_CACHE: dict[str, Any] = {"key": None, "ids": None, "ts": 0.0}
_UNION_CACHE_TTL_SEC = 15.0

KEY_PLACEMENT = "strategy_ui_placement"
KEY_ENABLED = "enabled_strategies"
KEY_BOT_LIMITS = "bot_limits"
KEY_TEST_SETTINGS = "test_strategy_settings"
KEY_MAX_PER = "max_open_trades_per_strategy"
KEY_DUAL_HEDGE = "dual_hedge"
# Shared Strategy stack (admin): desired StaticPairList — source of truth vs scanners.
KEY_STRATEGY_WL = "strategy_pair_whitelist"
KEY_STRATEGY_SCAN_POLICY = "strategy_scan_policy"

DEFAULT_PLACEMENT: dict[str, Any] = {
    "main": [],
    "hidden": [],
    "_note": (
        "main = Strategies panel; not in main and not in hidden = Тестовые; "
        "hidden = neither."
    ),
    "_hidden_disabled": True,
}

DEFAULT_TEST_SETTINGS: dict[str, Any] = {
    "max_open_trades": 8,
    "max_open_trades_per_strategy": 2,
    "stake_amount": 5.0,
    "stoploss": 0.02,
    "take_profit": 0.02,
}

DEFAULT_STRATEGY_SCAN_POLICY: dict[str, Any] = {
    "apply_whitelist": False,
    "allow_shrink": False,
    "min_keep_pairs": 40,
}


def settings_owner_id(user: dict[str, Any] | None = None) -> str:
    """Resolve prefs owner: tenant id, or admin."""
    if user is None:
        try:
            import tenant_context as tc

            user = tc.current_user()
        except Exception:
            user = None
    if not user:
        return "admin"
    if tm.is_admin(user) and not tm.is_impersonating(user):
        return "admin"
    return str(user.get("id") or "admin")


def tenant_dir_for(user_id: str, *, base: Path | None = None) -> Path:
    root = Path(base) if base is not None else Path(tm.BASE)
    if user_id == "admin":
        return root / "user_data"
    return tm.tenant_user_data(user_id)


def _read_json_file(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_cache(path: Path, payload: Any) -> None:
    """Best-effort mirror to JSON for tools that still read files."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log.debug("json cache write failed %s: %s", path, exc)


def get_or_migrate(
    user_id: str,
    key: str,
    *,
    json_name: str,
    default: Any,
    base: Path | None = None,
) -> Any:
    """Load from DB; if missing, migrate from JSON once then persist."""
    uid = str(user_id)
    # Single DB read (avoid has_user_setting + get_user_setting double connect).
    val = cp.get_user_setting(uid, key, None)
    if val is not None:
        return val
    if cp.has_user_setting(uid, key):
        # Explicit null/empty JSON stored
        return default

    path = tenant_dir_for(uid, base=base) / json_name
    raw = _read_json_file(path, None)
    if raw is None:
        cp.set_user_setting(uid, key, default)
        return default
    cp.set_user_setting(uid, key, raw)
    return raw


def put(
    user_id: str,
    key: str,
    value: Any,
    *,
    json_name: str | None = None,
    base: Path | None = None,
) -> Any:
    uid = str(user_id)
    cp.set_user_setting(uid, key, value)
    if json_name:
        _write_json_cache(tenant_dir_for(uid, base=base) / json_name, value)
    return value


def default_placement() -> dict[str, Any]:
    return dict(DEFAULT_PLACEMENT)


def load_placement(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_PLACEMENT,
        json_name="strategy_ui_placement.json",
        default=default_placement(),
        base=base,
    )
    if not isinstance(raw, dict):
        return default_placement()
    main = [str(x) for x in (raw.get("main") or []) if str(x).strip()]
    hidden = [str(x) for x in (raw.get("hidden") or []) if str(x).strip()]
    # Legacy bulk-hidden pack with empty main → reset to all-in-test
    if not main and len(hidden) >= 20:
        cleaned = default_placement()
        put(
            user_id,
            KEY_PLACEMENT,
            cleaned,
            json_name="strategy_ui_placement.json",
            base=base,
        )
        return cleaned
    return {
        "main": main,
        "hidden": hidden,
        "_note": str(raw.get("_note") or DEFAULT_PLACEMENT["_note"]),
        "_hidden_disabled": bool(raw.get("_hidden_disabled", True)),
    }


def save_placement(
    user_id: str, data: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    main = [str(x).strip() for x in (data.get("main") or []) if str(x).strip()]
    seen = set(main)
    hidden = [
        str(x).strip()
        for x in (data.get("hidden") or [])
        if str(x).strip() and str(x).strip() not in seen
    ]
    out = {
        "main": main,
        "hidden": hidden,
        "_note": str(data.get("_note") or DEFAULT_PLACEMENT["_note"]),
        "_hidden_disabled": bool(data.get("_hidden_disabled", True)),
    }
    return put(
        user_id, KEY_PLACEMENT, out, json_name="strategy_ui_placement.json", base=base
    )


def load_enabled_payload(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_ENABLED,
        json_name="enabled_strategies.json",
        default={"enabled": {}, "inverted": {}, "trained_risk": {}, "ml_confidence": {}},
        base=base,
    )
    if not isinstance(raw, dict):
        return {"enabled": {}, "inverted": {}, "trained_risk": {}, "ml_confidence": {}}
    return raw


def save_enabled_payload(
    user_id: str, payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    return put(user_id, KEY_ENABLED, payload, json_name="enabled_strategies.json", base=base)


def load_bot_limits_payload(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_BOT_LIMITS,
        json_name="bot_limits.json",
        default={},
        base=base,
    )
    return raw if isinstance(raw, dict) else {}


def save_bot_limits_payload(
    user_id: str, payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    return put(user_id, KEY_BOT_LIMITS, payload, json_name="bot_limits.json", base=base)


def load_test_settings(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_TEST_SETTINGS,
        json_name="test_strategy_settings.json",
        default=dict(DEFAULT_TEST_SETTINGS),
        base=base,
    )
    if not isinstance(raw, dict):
        return dict(DEFAULT_TEST_SETTINGS)
    out = dict(DEFAULT_TEST_SETTINGS)
    out.update(raw)
    return out


def save_test_settings(
    user_id: str, payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    return put(
        user_id, KEY_TEST_SETTINGS, payload, json_name="test_strategy_settings.json", base=base
    )


def load_max_per_strategy(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_MAX_PER,
        json_name="max_open_trades_per_strategy.json",
        default={"value": 0},
        base=base,
    )
    return raw if isinstance(raw, dict) else {"value": 0}


def save_max_per_strategy(
    user_id: str, payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    return put(
        user_id,
        KEY_MAX_PER,
        payload,
        json_name="max_open_trades_per_strategy.json",
        base=base,
    )


def load_dual_hedge(user_id: str, *, base: Path | None = None) -> dict[str, Any]:
    raw = get_or_migrate(
        user_id,
        KEY_DUAL_HEDGE,
        json_name="dual_hedge.json",
        default={"enabled": False},
        base=base,
    )
    return raw if isinstance(raw, dict) else {"enabled": False}


def save_dual_hedge(
    user_id: str, payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    return put(user_id, KEY_DUAL_HEDGE, payload, json_name="dual_hedge.json", base=base)


def _normalize_pairs(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        p = str(item or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def load_strategy_scan_policy(*, base: Path | None = None) -> dict[str, Any]:
    """Admin policy: scanners must not overwrite a large StaticPairList by default."""
    root = Path(base) if base is not None else Path(tm.BASE)
    raw = get_or_migrate(
        "admin",
        KEY_STRATEGY_SCAN_POLICY,
        json_name="strategy_scan_policy.json",
        default=dict(DEFAULT_STRATEGY_SCAN_POLICY),
        base=root,
    )
    out = dict(DEFAULT_STRATEGY_SCAN_POLICY)
    if isinstance(raw, dict):
        out["apply_whitelist"] = bool(raw.get("apply_whitelist", False))
        out["allow_shrink"] = bool(raw.get("allow_shrink", False))
        try:
            out["min_keep_pairs"] = max(1, int(raw.get("min_keep_pairs") or 40))
        except (TypeError, ValueError):
            out["min_keep_pairs"] = 40
    # Mirror into strategy_scan_config.json so CLI scanner stays in sync.
    try:
        sc_path = root / "user_data" / "strategy_scan_config.json"
        if sc_path.is_file():
            sc = _read_json_file(sc_path, {}) or {}
            if isinstance(sc, dict) and bool(sc.get("apply_whitelist")) != out["apply_whitelist"]:
                sc["apply_whitelist"] = out["apply_whitelist"]
                _write_json_cache(sc_path, sc)
    except Exception:
        pass
    return out


def save_strategy_scan_policy(
    payload: dict[str, Any], *, base: Path | None = None
) -> dict[str, Any]:
    root = Path(base) if base is not None else Path(tm.BASE)
    cur = load_strategy_scan_policy(base=root)
    if "apply_whitelist" in payload:
        cur["apply_whitelist"] = bool(payload.get("apply_whitelist"))
    if "allow_shrink" in payload:
        cur["allow_shrink"] = bool(payload.get("allow_shrink"))
    if "min_keep_pairs" in payload:
        try:
            cur["min_keep_pairs"] = max(1, int(payload.get("min_keep_pairs") or 40))
        except (TypeError, ValueError):
            pass
    put(
        "admin",
        KEY_STRATEGY_SCAN_POLICY,
        cur,
        json_name="strategy_scan_policy.json",
        base=root,
    )
    sc_path = root / "user_data" / "strategy_scan_config.json"
    if sc_path.is_file():
        sc = _read_json_file(sc_path, {}) or {}
        if isinstance(sc, dict):
            sc["apply_whitelist"] = cur["apply_whitelist"]
            _write_json_cache(sc_path, sc)
    return cur


def _seed_strategy_whitelist_from_files(root: Path) -> list[str]:
    """Prefer the largest of signal-engine / strategy / grid StaticPairLists."""
    candidates: list[list[str]] = []
    for name in (
        "config_signal_engine.json",
        "config_strategy.json",
        "config_grid.json",
    ):
        path = root / "user_data" / name
        raw = _read_json_file(path, None)
        if not isinstance(raw, dict):
            continue
        wl = _normalize_pairs((raw.get("exchange") or {}).get("pair_whitelist"))
        if wl:
            candidates.append(wl)
    if not candidates:
        return []
    return max(candidates, key=len)


def load_strategy_whitelist(*, base: Path | None = None) -> dict[str, Any]:
    """Desired Strategy whitelist (shared stack) — DB is source of truth."""
    root = Path(base) if base is not None else Path(tm.BASE)
    raw = cp.get_user_setting("admin", KEY_STRATEGY_WL, None)
    if isinstance(raw, dict) and _normalize_pairs(raw.get("pairs")):
        return {
            "pairs": _normalize_pairs(raw.get("pairs")),
            "protected": bool(raw.get("protected", True)),
            "source": str(raw.get("source") or "db"),
        }
    seeded = _seed_strategy_whitelist_from_files(root)
    out = {"pairs": seeded, "protected": True, "source": "seed"}
    if seeded:
        put(
            "admin",
            KEY_STRATEGY_WL,
            out,
            json_name="strategy_pair_whitelist.json",
            base=root,
        )
    return out


def save_strategy_whitelist(
    pairs: list[str],
    *,
    source: str = "manual",
    protected: bool = True,
    base: Path | None = None,
) -> dict[str, Any]:
    root = Path(base) if base is not None else Path(tm.BASE)
    out = {
        "pairs": _normalize_pairs(pairs),
        "protected": bool(protected),
        "source": str(source or "manual"),
    }
    return put(
        "admin",
        KEY_STRATEGY_WL,
        out,
        json_name="strategy_pair_whitelist.json",
        base=root,
    )


def enabled_map_for(
    user_id: str, strategy_ids: list[str], *, base: Path | None = None
) -> dict[str, bool]:
    payload = load_enabled_payload(user_id, base=base)
    stored = payload.get("enabled") if isinstance(payload.get("enabled"), dict) else {}
    return {sid: bool(stored.get(sid, False)) for sid in strategy_ids}


def union_enabled_strategy_ids(
    strategy_ids: list[str], trader_ids: list[str]
) -> list[str]:
    """Strategies enabled by at least one active trader (for signal-engine)."""
    if not trader_ids:
        return []
    cache_key = (tuple(strategy_ids), tuple(trader_ids))
    now = time.monotonic()
    with _UNION_CACHE_LOCK:
        if (
            _UNION_CACHE["key"] == cache_key
            and _UNION_CACHE["ids"] is not None
            and (now - float(_UNION_CACHE["ts"])) < _UNION_CACHE_TTL_SEC
        ):
            return list(_UNION_CACHE["ids"])
    on: set[str] = set()
    for uid in trader_ids:
        emap = enabled_map_for(uid, strategy_ids)
        for sid, flag in emap.items():
            if flag:
                on.add(sid)
    result = [sid for sid in strategy_ids if sid in on]
    with _UNION_CACHE_LOCK:
        _UNION_CACHE["key"] = cache_key
        _UNION_CACHE["ids"] = list(result)
        _UNION_CACHE["ts"] = now
    return result


def seed_new_user_prefs(user_id: str, *, base: Path | None = None) -> None:
    """Fresh tenant: all strategies in test, all OFF, default limits."""
    save_placement(user_id, default_placement(), base=base)
    save_enabled_payload(
        user_id,
        {"enabled": {}, "inverted": {}, "trained_risk": {}, "ml_confidence": {}},
        base=base,
    )
    save_test_settings(user_id, dict(DEFAULT_TEST_SETTINGS), base=base)
    save_dual_hedge(user_id, {"enabled": False}, base=base)
    save_max_per_strategy(user_id, {"value": 0}, base=base)
