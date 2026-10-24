#!/usr/bin/env python3
"""Adaptive scan scheduler — faster scans when bot trade slots are not full."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Share pair-config file handler (pair_config logger is wired in pair_config_server).
log = logging.getLogger("pair_config.adaptive_scan")

# Minutes between scan passes; after 30 min cycle resets to 5.
SCAN_INTERVALS_MIN = (5, 10, 15, 30)
# Fixed-mode choices exposed in UI settings.
ALLOWED_FIXED_INTERVALS_MIN = (5, 10, 15, 30, 60)
# ML Finder on 5m candles: stride * 5 min between scans.
FINDER_STRIDE_BY_MIN = {5: 1, 10: 2, 15: 3, 30: 6, 60: 12}
DEFAULT_FINDER_STRIDE = 6  # 30 min when all slots full
DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "adaptive",  # adaptive | fixed
    "fixed_interval_min": 15,
}
SETTINGS_DB_KEY = "adaptive_scan"

COUNT_URLS = {
    "finder": "http://127.0.0.1:8080/api/v1/count",
    "strategy": "http://127.0.0.1:8081/api/v1/count",
    "grid": "http://127.0.0.1:8082/api/v1/count",
}
SHOW_CONFIG_URLS = {
    "finder": "http://127.0.0.1:8080/api/v1/show_config",
    "strategy": "http://127.0.0.1:8081/api/v1/show_config",
    "grid": "http://127.0.0.1:8082/api/v1/show_config",
}
BOTS = ("finder", "strategy", "grid")
# bot -> True after one "not running" note (avoid log spam while UI polls /adaptive-scan)
_STOPPED_NOTED: dict[str, bool] = {}

_STATE: dict[str, Any] | None = None
_STATE_LOCK = threading.Lock()
_SETTINGS_CACHE: dict[str, Any] | None = None
_SETTINGS_CACHE_LOCK = threading.Lock()


def _auth_header() -> str:
    import base64

    user = os.environ.get("FREQUI_USERNAME", "cryptotools")
    password = os.environ.get("FREQUI_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _api_get(url: str) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Authorization": _auth_header()}, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        log.debug("api get %s: %s", url, exc)
        return None


def _state_path(base: Path) -> Path:
    return base / "user_data" / "adaptive_scan_state.json"


def _settings_path(base: Path) -> Path:
    """Legacy JSON path — migrated once into control_plane.sqlite."""
    return base / "user_data" / "adaptive_scan_settings.json"


def _normalize_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        data.update(raw)
    mode = str(data.get("mode") or "adaptive").strip().lower()
    if mode not in ("adaptive", "fixed"):
        mode = "adaptive"
    try:
        fixed = int(data.get("fixed_interval_min") or DEFAULT_SETTINGS["fixed_interval_min"])
    except (TypeError, ValueError):
        fixed = int(DEFAULT_SETTINGS["fixed_interval_min"])
    if fixed not in ALLOWED_FIXED_INTERVALS_MIN:
        # Nearest allowed value.
        fixed = min(ALLOWED_FIXED_INTERVALS_MIN, key=lambda x: abs(x - fixed))
    return {"mode": mode, "fixed_interval_min": fixed}


def _control_plane():
    import control_plane as cp

    return cp


def _migrate_settings_json_to_db(base: Path) -> dict[str, Any] | None:
    path = _settings_path(base)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    normalized = _normalize_settings(raw)
    try:
        _control_plane().set_app_setting(
            SETTINGS_DB_KEY,
            normalized,
            updated_by="migrate_json",
        )
        path.rename(path.with_suffix(".json.migrated"))
        log.info("migrated adaptive_scan_settings.json → control_plane.sqlite")
    except Exception as exc:  # noqa: BLE001
        log.warning("adaptive scan settings migrate failed: %s", exc)
    return normalized


def load_adaptive_scan_settings(base: Path) -> dict[str, Any]:
    global _SETTINGS_CACHE
    with _SETTINGS_CACHE_LOCK:
        if _SETTINGS_CACHE is not None:
            return dict(_SETTINGS_CACHE)

    cp = _control_plane()
    try:
        raw = cp.get_app_setting(SETTINGS_DB_KEY, None)
    except Exception as exc:  # noqa: BLE001
        log.debug("app_settings read failed: %s", exc)
        raw = None

    if raw is None:
        migrated = _migrate_settings_json_to_db(base)
        raw = migrated

    normalized = _normalize_settings(raw if isinstance(raw, dict) else None)
    # Seed DB if empty so UI/API always have a row.
    if raw is None:
        try:
            cp.set_app_setting(SETTINGS_DB_KEY, normalized, updated_by="default")
        except Exception as exc:  # noqa: BLE001
            log.debug("app_settings seed failed: %s", exc)

    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE = dict(normalized)
    return dict(normalized)


def save_adaptive_scan_settings(
    base: Path,
    settings: dict[str, Any],
    *,
    updated_by: str | None = None,
) -> dict[str, Any]:
    global _SETTINGS_CACHE
    normalized = _normalize_settings(settings)
    _control_plane().set_app_setting(
        SETTINGS_DB_KEY,
        normalized,
        updated_by=updated_by or "api",
    )
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE = dict(normalized)

    # Apply immediately: next tick uses new interval without waiting out the old one.
    with _STATE_LOCK:
        state = _load_state(base)
        state["step"] = 0
        state["last_run_at"] = None
        state["last_interval_min"] = (
            int(normalized["fixed_interval_min"])
            if normalized["mode"] == "fixed"
            else SCAN_INTERVALS_MIN[0]
        )
        _save_state(base, state)

    log.info(
        "adaptive scan settings (db): mode=%s fixed=%s min",
        normalized["mode"],
        normalized["fixed_interval_min"],
    )
    return normalized


def set_adaptive_scan_settings(
    base: Path,
    patch: dict[str, Any] | None = None,
    *,
    updated_by: str | None = None,
) -> dict[str, Any]:
    current = load_adaptive_scan_settings(base)
    if isinstance(patch, dict):
        # Only known keys — ignore UI noise.
        if "mode" in patch:
            current["mode"] = patch["mode"]
        if "fixed_interval_min" in patch:
            current["fixed_interval_min"] = patch["fixed_interval_min"]
    return save_adaptive_scan_settings(base, current, updated_by=updated_by)


def _intervals_for(base: Path) -> tuple[int, ...]:
    settings = load_adaptive_scan_settings(base)
    if settings["mode"] == "fixed":
        return (int(settings["fixed_interval_min"]),)
    return SCAN_INTERVALS_MIN


def _finder_stride_for(interval_min: int) -> int:
    if interval_min in FINDER_STRIDE_BY_MIN:
        return FINDER_STRIDE_BY_MIN[interval_min]
    return max(1, int(round(interval_min / 5)))


def _load_state(base: Path) -> dict[str, Any]:
    path = _state_path(base)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "step": 0,
        "last_run_at": None,
        "last_interval_min": SCAN_INTERVALS_MIN[0],
        "finder_scan_stride": DEFAULT_FINDER_STRIDE,
    }


def _save_state(base: Path, state: dict[str, Any]) -> None:
    path = _state_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _pairlist_mode(base: Path) -> str:
    path = base / "user_data" / "pairlist_mode.json"
    if not path.is_file():
        return "scanner"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("mode", "scanner")
    except (json.JSONDecodeError, OSError):
        return "scanner"


def get_bot_slots(base: Path) -> dict[str, Any]:
    per_bot: dict[str, dict[str, int]] = {}
    open_total = 0
    max_total = 0
    for bot in BOTS:
        cfg = _api_get(SHOW_CONFIG_URLS[bot]) or {}
        state = str(cfg.get("state") or "").lower()
        # Process down or trader STOPPED: /count would 502-spam bot logs — skip.
        if not cfg or (state and state != "running"):
            note_state = state or "offline"
            if not _STOPPED_NOTED.get(bot):
                _STOPPED_NOTED[bot] = True
                log.info("%s not running (state=%s) — skip /count until start", bot, note_state)
            per_bot[bot] = {"open": 0, "max": 0, "free": 0, "state": note_state}
            continue
        _STOPPED_NOTED.pop(bot, None)
        data = _api_get(COUNT_URLS[bot]) or {}
        current = int(data.get("current") or 0)
        maximum = int(data.get("max") or 0)
        per_bot[bot] = {
            "open": current,
            "max": maximum,
            "free": max(0, maximum - current),
            "state": state or "running",
        }
        open_total += current
        max_total += maximum
    return {
        "open_total": open_total,
        "max_total": max_total,
        "free_total": max(0, max_total - open_total),
        "full": max_total > 0 and open_total >= max_total,
        "bots": per_bot,
        "pairlist_mode": _pairlist_mode(base),
    }


def _set_finder_scan_stride(base: Path, stride: int) -> bool:
    path = base / "user_data" / "trade_finder.json"
    if not path.is_file():
        return False
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    stride = max(1, int(stride))
    if int(cfg.get("scan_stride") or 0) == stride:
        return False
    cfg["scan_stride"] = stride
    path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("finder scan_stride -> %s (%s min)", stride, stride * 5)
    return True


def _run_pair_scans(base: Path, slots: dict[str, Any], *, ranging_scan, strategy_scan) -> list[str]:
    """Run external pair scanners when in scanner whitelist mode."""
    if slots.get("pairlist_mode") != "scanner":
        return []
    ran: list[str] = []
    bots = slots.get("bots") or {}
    if bots.get("grid", {}).get("free", 0) > 0:
        try:
            ranging_scan()
            ran.append("ranging")
        except Exception as exc:
            log.warning("ranging scan: %s", exc)
    if bots.get("strategy", {}).get("free", 0) > 0:
        try:
            strategy_scan()
            ran.append("strategy")
        except Exception as exc:
            log.warning("strategy scan: %s", exc)
    return ran


def run_adaptive_scan_tick(
    base: Path,
    *,
    ranging_scan,
    strategy_scan,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single scheduler tick. Returns status snapshot."""
    if state is None:
        state = _load_state(base)

    settings = load_adaptive_scan_settings(base)
    intervals = _intervals_for(base)
    slots = get_bot_slots(base)
    step = int(state.get("step") or 0) % len(intervals)
    interval_min = intervals[step]

    out: dict[str, Any] = {
        "slots": slots,
        "settings": settings,
        "mode": settings["mode"],
        "step": step,
        "interval_min": interval_min,
        "next_interval_min": intervals[(step + 1) % len(intervals)],
        "finder_scan_stride": int(state.get("finder_scan_stride") or DEFAULT_FINDER_STRIDE),
        "last_run_at": state.get("last_run_at"),
        "action": "idle",
    }

    if slots["max_total"] <= 0:
        out["action"] = "no_capacity"
        return out

    if slots["full"]:
        state["step"] = 0
        state["last_interval_min"] = intervals[0]
        if _set_finder_scan_stride(base, DEFAULT_FINDER_STRIDE):
            state["finder_scan_stride"] = DEFAULT_FINDER_STRIDE
        _save_state(base, state)
        out["step"] = 0
        out["interval_min"] = intervals[0]
        out["finder_scan_stride"] = DEFAULT_FINDER_STRIDE
        out["action"] = "slots_full"
        return out

    now = time.time()
    last_run = float(state.get("last_run_at") or 0)
    if last_run and (now - last_run) < interval_min * 60:
        out["action"] = "waiting"
        out["seconds_until_next"] = int(interval_min * 60 - (now - last_run))
        return out

    open_before = slots["open_total"]
    stride = _finder_stride_for(interval_min)
    if _set_finder_scan_stride(base, stride):
        state["finder_scan_stride"] = stride

    scans_ran = _run_pair_scans(base, slots, ranging_scan=ranging_scan, strategy_scan=strategy_scan)

    time.sleep(3)
    slots_after = get_bot_slots(base)
    open_after = slots_after["open_total"]

    state["last_run_at"] = now
    state["last_interval_min"] = interval_min

    if slots_after["full"]:
        state["step"] = 0
        _set_finder_scan_stride(base, DEFAULT_FINDER_STRIDE)
        state["finder_scan_stride"] = DEFAULT_FINDER_STRIDE
        out["action"] = "scan_filled_all"
    elif settings["mode"] == "fixed":
        state["step"] = 0
        out["action"] = "scan_done"
    else:
        state["step"] = (step + 1) % len(intervals)
        out["action"] = "scan_done"

    _save_state(base, state)

    next_intervals = _intervals_for(base)
    next_step = int(state["step"]) % len(next_intervals)
    out.update(
        {
            "step": next_step,
            "interval_min": next_intervals[next_step],
            "finder_scan_stride": state.get("finder_scan_stride", DEFAULT_FINDER_STRIDE),
            "open_before": open_before,
            "open_after": open_after,
            "scans_ran": scans_ran,
            "slots_after": slots_after,
        }
    )
    log.info(
        "adaptive scan %s · mode=%s · open %s->%s · next in %s min · finder stride %s",
        out["action"],
        settings["mode"],
        open_before,
        open_after,
        out["interval_min"],
        out["finder_scan_stride"],
    )
    return out


def get_adaptive_scan_status(base: Path) -> dict[str, Any]:
    state = _load_state(base)
    settings = load_adaptive_scan_settings(base)
    intervals = _intervals_for(base)
    slots = get_bot_slots(base)
    step = int(state.get("step") or 0) % len(intervals)
    interval_min = intervals[step]
    last_run = float(state.get("last_run_at") or 0)
    seconds_until = None
    if not slots.get("full") and last_run:
        left = interval_min * 60 - (time.time() - last_run)
        if left > 0:
            seconds_until = int(left)
    return {
        "enabled": True,
        "mode": settings["mode"],
        "settings": settings,
        "allowed_fixed_intervals_min": list(ALLOWED_FIXED_INTERVALS_MIN),
        "intervals_min": list(intervals),
        "adaptive_intervals_min": list(SCAN_INTERVALS_MIN),
        "step": step,
        "current_interval_min": interval_min,
        "finder_scan_stride": int(state.get("finder_scan_stride") or DEFAULT_FINDER_STRIDE),
        "last_run_at": state.get("last_run_at"),
        "seconds_until_next": seconds_until,
        "slots": slots,
    }


def start_adaptive_scan_scheduler(
    base: Path,
    *,
    ranging_scan,
    strategy_scan,
    poll_sec: int = 60,
) -> None:
    """Background loop — call from pair_config_server after imports are ready."""

    def _loop() -> None:
        log.info("adaptive scan scheduler started (intervals %s min)", SCAN_INTERVALS_MIN)
        while True:
            try:
                with _STATE_LOCK:
                    global _STATE
                    status = run_adaptive_scan_tick(
                        base,
                        ranging_scan=ranging_scan,
                        strategy_scan=strategy_scan,
                    )
                    _STATE = status
            except Exception as exc:
                log.warning("adaptive scan tick failed: %s", exc)
            time.sleep(max(30, poll_sec))

    thread = threading.Thread(target=_loop, name="adaptive-scan", daemon=True)
    thread.start()
