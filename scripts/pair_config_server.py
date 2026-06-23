#!/usr/bin/env python3
"""Lightweight pair whitelist admin for both Freqtrade bots."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

BASE = Path(os.environ.get("FT_BASE", "/home/freqtrade/freqtrade"))
STRATEGIES_DIR = BASE / "user_data" / "strategies"
ENABLED_STRATEGIES_FILE = BASE / "user_data" / "enabled_strategies.json"
BOT_LIMITS_FILE = BASE / "user_data" / "bot_limits.json"
BOT_STRATEGIES_FILE = BASE / "user_data" / "bot_strategies.json"
DEFAULT_BOT_LIMITS = {"freqai": 3, "strategy": 2, "grid": 2}
ROUTER_STRATEGY = "MultiStrategyRouter"
CONFIGS = {
    "freqai": BASE / "user_data" / "config.json",
    "strategy": BASE / "user_data" / "config_strategy.json",
    "grid": BASE / "user_data" / "config_grid.json",
}
RELOAD = {
    "freqai": "http://127.0.0.1:8080/api/v1/reload_config",
    "strategy": "http://127.0.0.1:8081/api/v1/reload_config",
    "grid": "http://127.0.0.1:8082/api/v1/reload_config",
}
BLACKLIST = {
    "freqai": "http://127.0.0.1:8080/api/v1/blacklist",
    "strategy": "http://127.0.0.1:8081/api/v1/blacklist",
    "grid": "http://127.0.0.1:8082/api/v1/blacklist",
}
WHITELIST = {
    "freqai": "http://127.0.0.1:8080/api/v1/whitelist",
    "strategy": "http://127.0.0.1:8081/api/v1/whitelist",
    "grid": "http://127.0.0.1:8082/api/v1/whitelist",
}

AUTH_USER = os.environ.get("FREQUI_USERNAME", "freqtrader")
AUTH_PASS = os.environ.get("FREQUI_PASSWORD", "")

MIN_MAX_TRADES = 1
MAX_MAX_TRADES = 10

AVAILABLE_STRATEGIES = [
    {
        "id": "CriptoPairsStrategy",
        "name": "RSI + EMA + Bollinger",
        "desc": (
            "Консервативная стратегия на откатах. "
            "Лонг: RSI ниже 35, быстрая EMA выше медленной, цена у нижней полосы Bollinger. "
            "Шорт: RSI выше 65, EMA направлена вниз, цена у верхней полосы. "
            "Условия строгие — сделки бывают редко."
        ),
    },
    {
        "id": "SupertrendStrategy",
        "name": "Supertrend (тренд по ATR)",
        "desc": (
            "Следует за индикатором Supertrend. "
            "Лонг при смене тренда вверх, шорт при смене вниз; RSI отсекает слабые сигналы. "
            "Хорошо подходит для выраженных трендовых движений на 5m."
        ),
    },
    {
        "id": "MacdEmaStrategy",
        "name": "MACD + EMA 200",
        "desc": (
            "Классическое сочетание: пересечение линий MACD в сторону долгосрочного тренда. "
            "Лонг — MACD вверх и цена выше EMA 200; шорт — MACD вниз и цена ниже EMA 200."
        ),
    },
    {
        "id": "TripleEmaStrategy",
        "name": "Тройная EMA (8 / 21 / 55)",
        "desc": (
            "Тренд-следование по стеку скользящих. "
            "Лонг при бычьем порядке EMA (8 > 21 > 55) и пересечении быстрой вверх; "
            "шорт при медвежьем стеке и пересечении вниз."
        ),
    },
    {
        "id": "BollingerRsiStrategy",
        "name": "Bollinger + RSI (отбой)",
        "desc": (
            "Стратегия возврата к среднему. "
            "Лонг от нижней полосы Bollinger при низком RSI; шорт от верхней полосы при высоком RSI. "
            "Лучше работает во флэте и боковике."
        ),
    },
    {
        "id": "AdxMomentumStrategy",
        "name": "ADX — импульс тренда",
        "desc": (
            "Входит только при сильном тренде (ADX > 25). "
            "Лонг — когда DI+ пересекает DI− снизу; шорт — наоборот. "
            "Фильтр EMA 21 подтверждает направление."
        ),
    },
]


def normalize_pair(pair: str) -> str:
    pair = pair.strip().upper()
    if not pair:
        raise ValueError("empty pair")
    if ":" not in pair and pair.endswith("/USDT"):
        pair = f"{pair}:USDT"
    if "/" not in pair:
        pair = f"{pair}/USDT:USDT"
    return pair


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def load_bot_limits() -> dict[str, int]:
    """Read persistent max_open_trades per bot."""
    if not BOT_LIMITS_FILE.is_file():
        return ensure_bot_limits_snapshot()
    data = json.loads(BOT_LIMITS_FILE.read_text(encoding="utf-8"))
    stored = data.get("max_open_trades", data)
    limits = dict(DEFAULT_BOT_LIMITS)
    for bot in CONFIGS:
        if bot in stored:
            limits[bot] = int(stored[bot])
    return limits


def snapshot_limits_from_configs() -> dict[str, int]:
    """Read max_open_trades from live config files and persist to bot_limits.json."""
    limits: dict[str, int] = {}
    for bot, path in CONFIGS.items():
        try:
            cfg = load_config(path)
            raw = cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2))
            if raw == float("inf"):
                raw = MAX_MAX_TRADES
            limits[bot] = int(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            limits[bot] = DEFAULT_BOT_LIMITS.get(bot, 2)
    save_bot_limits(limits)
    return limits


def ensure_bot_limits_snapshot() -> dict[str, int]:
    """Create bot_limits.json from configs only when missing."""
    if BOT_LIMITS_FILE.is_file():
        return load_bot_limits()
    return snapshot_limits_from_configs()


def save_bot_limits(limits: dict[str, int]) -> None:
    BOT_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_LIMITS_FILE.with_suffix(".json.tmp")
    payload = {"max_open_trades": {bot: int(limits[bot]) for bot in CONFIGS if bot in limits}}
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(BOT_LIMITS_FILE)


def apply_bot_limits_to_configs() -> dict[str, int]:
    """Write saved limits into all bot config files."""
    limits = load_bot_limits()
    for bot, path in CONFIGS.items():
        if bot not in limits or not path.is_file():
            continue
        value = int(limits[bot])
        cfg = load_config(path)
        if cfg.get("max_open_trades") != value:
            cfg["max_open_trades"] = value
            save_config(path, cfg)
    return limits

RELOAD_RETRIES = 8
RELOAD_RETRY_DELAY = 2.0


def _is_transient_api_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return True
        if isinstance(reason, OSError) and getattr(reason, "errno", None) == 111:
            return True
        return False
    if isinstance(exc, (ConnectionRefusedError, TimeoutError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (111, 110, 104):
        return True
    return False


def api_call(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    *,
    retries: int = 1,
    retry_delay: float = 1.0,
) -> Any:
    last_exc: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            data = None
            headers = {"Authorization": _basic_header()}
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 >= retries or not _is_transient_api_error(exc):
                raise
            time.sleep(retry_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return None


_CPU_SAMPLE: tuple[int, int] | None = None


def _cpu_jiffies() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as fh:
        parts = [int(x) for x in fh.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return sum(parts), idle


def _memory_stats() -> dict[str, float]:
    info: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, val = line.split(":", 1)
            info[key.strip()] = int(val.split()[0])
    total_kb = info["MemTotal"]
    avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
    used_kb = total_kb - avail_kb
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total_mb": round(total_kb / 1024),
        "used_mb": round(used_kb / 1024),
        "available_mb": round(avail_kb / 1024),
        "used_pct": round(100 * used_kb / total_kb, 1) if total_kb else 0.0,
        "swap_used_mb": round((swap_total - swap_free) / 1024, 1),
    }


def _load_level(load_1m: float, cpus: int) -> str:
    ratio = load_1m / max(cpus, 1)
    if ratio < 0.75:
        return "ok"
    if ratio < 1.25:
        return "warn"
    return "high"


def get_system_stats() -> dict[str, Any]:
    global _CPU_SAMPLE
    cpus = os.cpu_count() or 1
    with open("/proc/loadavg", encoding="utf-8") as fh:
        load_parts = fh.read().split()
    load_1m, load_5m, load_15m = (float(load_parts[i]) for i in range(3))

    uptime_s = 0.0
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime_s = float(fh.read().split()[0])
    except OSError:
        pass

    cpu_percent: float | None = None
    try:
        sample = _cpu_jiffies()
        if _CPU_SAMPLE is not None:
            dt_total = sample[0] - _CPU_SAMPLE[0]
            dt_idle = sample[1] - _CPU_SAMPLE[1]
            if dt_total > 0:
                cpu_percent = round(100.0 * (1.0 - dt_idle / dt_total), 1)
        _CPU_SAMPLE = sample
    except OSError:
        pass

    mem = _memory_stats()
    disk = shutil.disk_usage("/")
    disk_used_pct = round(100 * disk.used / disk.total, 1) if disk.total else 0.0

    return {
        "cpu_percent": cpu_percent,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "cpus": cpus,
        "load_level": _load_level(load_1m, cpus),
        "memory": mem,
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "used_pct": disk_used_pct,
        },
        "uptime_seconds": int(uptime_s),
    }


def reload_bot(bot: str) -> Any:
    return api_call(
        RELOAD[bot],
        "POST",
        retries=RELOAD_RETRIES,
        retry_delay=RELOAD_RETRY_DELAY,
    )


def safe_get_state(bot: str) -> dict[str, Any]:
    try:
        return get_state(bot)
    except BaseException as exc:  # noqa: BLE001
        cfg = load_config(CONFIGS[bot])
        max_trades = cfg.get("max_open_trades", 1)
        if max_trades == float("inf"):
            max_trades = MAX_MAX_TRADES
        fallback: dict[str, Any] = {
            "bot": bot,
            "config_whitelist": list(cfg.get("exchange", {}).get("pair_whitelist", [])),
            "active_whitelist": [],
            "blacklist": [],
            "max_open_trades": int(max_trades),
            "api_unreachable": str(exc),
        }
        if bot == "strategy":
            enabled = load_enabled_map()
            fallback["strategy"] = ROUTER_STRATEGY
            fallback["enabled_strategies"] = enabled
            fallback["enabled_count"] = sum(1 for on in enabled.values() if on)
        return fallback


def _basic_header() -> str:
    import base64

    token = base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
    return f"Basic {token}"


def _strategy_ids() -> set[str]:
    return {s["id"] for s in AVAILABLE_STRATEGIES}


def get_strategy_catalog() -> list[dict[str, str]]:
    return list(AVAILABLE_STRATEGIES)


def validate_strategy(strategy_id: str) -> str:
    strategy_id = strategy_id.strip()
    if strategy_id not in _strategy_ids():
        raise ValueError("unknown strategy")
    path = STRATEGIES_DIR / f"{strategy_id}.py"
    if not path.is_file():
        raise ValueError(f"strategy file missing: {strategy_id}")
    return strategy_id


def default_enabled_map() -> dict[str, bool]:
    return {s["id"]: s["id"] == "CriptoPairsStrategy" for s in AVAILABLE_STRATEGIES}


def load_enabled_map() -> dict[str, bool]:
    if not ENABLED_STRATEGIES_FILE.is_file():
        return default_enabled_map()
    data = json.loads(ENABLED_STRATEGIES_FILE.read_text(encoding="utf-8"))
    enabled = data.get("enabled", {})
    result = default_enabled_map()
    for sid in result:
        if sid in enabled:
            result[sid] = bool(enabled[sid])
    return result


def save_enabled_map(enabled: dict[str, bool]) -> None:
    ENABLED_STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENABLED_STRATEGIES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({"enabled": enabled}, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(ENABLED_STRATEGIES_FILE)
    save_strategies_prefs(enabled)


def save_strategies_prefs(enabled: dict[str, bool]) -> None:
    BOT_STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_STRATEGIES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({"enabled": enabled}, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(BOT_STRATEGIES_FILE)


def snapshot_strategies_from_file() -> dict[str, bool]:
    """Persist current enabled strategies before deploy overwrites anything."""
    enabled = load_enabled_map()
    save_strategies_prefs(enabled)
    return enabled


def apply_strategies_prefs() -> dict[str, bool]:
    """Restore enabled strategies from bot_strategies.json after deploy."""
    if not BOT_STRATEGIES_FILE.is_file():
        return load_enabled_map()
    data = json.loads(BOT_STRATEGIES_FILE.read_text(encoding="utf-8"))
    stored = data.get("enabled", {})
    result = default_enabled_map()
    for sid in result:
        if sid in stored:
            result[sid] = bool(stored[sid])
    save_enabled_map(result)
    return result


def ensure_router_config() -> None:
    cfg = load_config(CONFIGS["strategy"])
    if cfg.get("strategy") != ROUTER_STRATEGY:
        cfg["strategy"] = ROUTER_STRATEGY
        save_config(CONFIGS["strategy"], cfg)


def get_strategies_payload() -> dict[str, Any]:
    enabled = load_enabled_map()
    enabled_ids = [sid for sid, on in enabled.items() if on]
    return {
        "router": ROUTER_STRATEGY,
        "enabled": enabled,
        "enabled_ids": enabled_ids,
        "enabled_count": len(enabled_ids),
        "strategies": get_strategy_catalog(),
    }


def toggle_strategy(strategy_id: str, enabled: bool) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    state = load_enabled_map()
    if not enabled:
        active = sum(1 for on in state.values() if on)
        if active <= 1 and state.get(strategy_id):
            raise ValueError("at least one strategy must stay enabled")
    state[strategy_id] = bool(enabled)
    save_enabled_map(state)
    ensure_router_config()
    # Router reads enabled_strategies.json on each signal — reload would restart the bot.
    return {
        "strategy": strategy_id,
        "enabled": state[strategy_id],
        **get_strategies_payload(),
    }


def get_state(bot: str) -> dict[str, Any]:
    cfg = load_config(CONFIGS[bot])
    config_pairs = list(cfg.get("exchange", {}).get("pair_whitelist", []))
    active = api_call(WHITELIST[bot])
    black = api_call(BLACKLIST[bot])
    max_trades = cfg.get("max_open_trades", 1)
    if max_trades == float("inf"):
        max_trades = MAX_MAX_TRADES
    state = {
        "bot": bot,
        "config_whitelist": config_pairs,
        "active_whitelist": active.get("whitelist", []),
        "blacklist": black.get("blacklist", []),
        "max_open_trades": int(max_trades),
    }
    if bot == "strategy":
        enabled = load_enabled_map()
        state["strategy"] = ROUTER_STRATEGY
        state["enabled_strategies"] = enabled
        state["enabled_count"] = sum(1 for on in enabled.values() if on)
    return state


def sync_freqai_config(mutator) -> list[str]:
    """Manual pair changes apply to FreqAI only (strategy/grid use scanners)."""
    path = CONFIGS["freqai"]
    cfg = load_config(path)
    wl = list(cfg.get("exchange", {}).get("pair_whitelist", []))
    wl = mutator(wl)
    cfg.setdefault("exchange", {})["pair_whitelist"] = wl
    save_config(path, cfg)
    return wl


def add_pair(pair: str) -> dict[str, Any]:
    pair = normalize_pair(pair)

    def add(wl: list[str]) -> list[str]:
        if pair not in wl:
            wl.append(pair)
        return wl

    sync_freqai_config(add)
    out: dict[str, Any] = {"pair": pair, "reloaded": {}}
    for bot in CONFIGS:
        try:
            q = urllib.parse.urlencode({"pairs_to_delete": pair})
            api_call(f"{BLACKLIST[bot]}?{q}", "DELETE")
        except urllib.error.HTTPError:
            pass
        try:
            out["reloaded"][bot] = reload_bot(bot)
        except BaseException as exc:  # noqa: BLE001
            out["reloaded"][bot] = {"error": str(exc)}
    out["state"] = {b: safe_get_state(b) for b in CONFIGS}
    return out


def set_max_open_trades(bot: str, value: int) -> dict[str, Any]:
    if bot not in CONFIGS:
        raise ValueError("bot must be freqai, strategy, or grid")
    value = int(value)
    if value < MIN_MAX_TRADES or value > MAX_MAX_TRADES:
        raise ValueError(
            f"max_open_trades must be between {MIN_MAX_TRADES} and {MAX_MAX_TRADES}"
        )

    cfg = load_config(CONFIGS[bot])
    cfg["max_open_trades"] = value
    save_config(CONFIGS[bot], cfg)
    limits = load_bot_limits()
    limits[bot] = value
    save_bot_limits(limits)
    reload_result: Any = None
    reload_warning: str | None = None
    try:
        reload_result = reload_bot(bot)
    except BaseException as exc:  # noqa: BLE001
        reload_warning = str(exc)
    result: dict[str, Any] = {
        "bot": bot,
        "max_open_trades": value,
        "reloaded": reload_result,
        "state": safe_get_state(bot),
    }
    if reload_warning:
        result["reload_warning"] = reload_warning
    return result


def set_strategy(strategy_id: str) -> dict[str, Any]:
    """Legacy: enable only one strategy, disable others."""
    strategy_id = validate_strategy(strategy_id)
    state = {sid: sid == strategy_id for sid in _strategy_ids()}
    save_enabled_map(state)
    ensure_router_config()
    return {
        "strategy": strategy_id,
        "state": safe_get_state("strategy"),
        **get_strategies_payload(),
    }


def remove_pair(pair: str) -> dict[str, Any]:
    pair = normalize_pair(pair)

    def remove(wl: list[str]) -> list[str]:
        return [p for p in wl if p != pair]

    sync_freqai_config(remove)
    out: dict[str, Any] = {"pair": pair, "blacklisted": {}}
    for bot in CONFIGS:
        out["blacklisted"][bot] = api_call(BLACKLIST[bot], "POST", {"blacklist": [pair]})
        try:
            out["blacklisted"][bot + "_reload"] = reload_bot(bot)
        except BaseException as exc:  # noqa: BLE001
            out["blacklisted"][bot + "_reload"] = {"error": str(exc)}
    out["state"] = {b: safe_get_state(b) for b in CONFIGS}
    return out


LOG_SOURCES: dict[str, tuple[str, Path]] = {
    "freqai": ("FreqAI", BASE / "user_data" / "logs" / "freqtrade-freqai.log"),
    "strategy": ("Стратегии", BASE / "user_data" / "logs" / "freqtrade-strategy.log"),
    "grid": ("Grid", BASE / "user_data" / "logs" / "freqtrade-grid.log"),
    "scanner": ("Сканер Grid", BASE / "user_data" / "logs" / "ranging-scanner.log"),
    "strategy_scanner": ("Сканер страт.", BASE / "user_data" / "logs" / "strategy-scanner.log"),
}


def _resolve_log_bots(requested: str) -> list[str]:
    if not requested or requested == "all":
        return list(LOG_SOURCES.keys())
    bots = [b.strip() for b in requested.split(",") if b.strip()]
    return [b for b in bots if b in LOG_SOURCES]


def _tail_file(path: Path, max_lines: int = 200) -> tuple[list[str], int]:
    if not path.is_file():
        return [], 0
    size = path.stat().st_size
    chunk = min(size, 512_000)
    with path.open("rb") as fh:
        fh.seek(max(0, size - chunk))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:], size


def _read_since(path: Path, pos: int) -> tuple[list[str], int]:
    if not path.is_file():
        return [], 0
    size = path.stat().st_size
    if size < pos:
        pos = 0
    if size <= pos:
        return [], size
    with path.open("rb") as fh:
        fh.seek(pos)
        data = fh.read()
    return data.decode("utf-8", errors="replace").splitlines(), size


def fetch_logs(
    bots: list[str],
    *,
    tail: int = 0,
    since: dict[str, int] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    positions: dict[str, int] = dict(since or {})

    for bot in bots:
        label, path = LOG_SOURCES[bot]
        if tail > 0 or bot not in positions:
            lines, pos = _tail_file(path, tail or 200)
            positions[bot] = pos
        else:
            lines, pos = _read_since(path, int(positions.get(bot, 0)))
            positions[bot] = pos
        for line in lines:
            if not line.strip():
                continue
            entries.append({"bot": bot, "label": label, "line": line})

    return {"entries": entries, "positions": positions}


RANGING_PAIRS_FILE = BASE / "user_data" / "ranging_pairs.json"
SCAN_SCRIPT = BASE / "scripts" / "scan_ranging_pairs.py"
SCAN_LOCK = BASE / "user_data" / ".ranging_scan.lock"
STRATEGY_PAIRS_FILE = BASE / "user_data" / "strategy_pairs.json"
STRATEGY_SCAN_SCRIPT = BASE / "scripts" / "scan_strategy_pairs.py"
STRATEGY_SCAN_LOCK = BASE / "user_data" / ".strategy_scan.lock"


def get_ranging_scan_status() -> dict[str, Any]:
    running = SCAN_LOCK.is_file()
    if running and time.time() - SCAN_LOCK.stat().st_mtime > 600:
        SCAN_LOCK.unlink(missing_ok=True)
        running = False
    if not RANGING_PAIRS_FILE.is_file():
        return {
            "scanned_at": None,
            "whitelist": [],
            "ranging_found": 0,
            "selected_count": 0,
            "candidates_checked": 0,
            "pairs": [],
            "running": running,
        }
    data = json.loads(RANGING_PAIRS_FILE.read_text(encoding="utf-8"))
    return {
        "scanned_at": data.get("scanned_at"),
        "whitelist": data.get("whitelist", []),
        "ranging_found": data.get("ranging_found", 0),
        "selected_count": data.get("selected_count", 0),
        "candidates_checked": data.get("candidates_checked", 0),
        "pairs": data.get("pairs", []),
        "running": running,
    }


def trigger_ranging_scan() -> dict[str, Any]:
    if SCAN_LOCK.is_file():
        age = time.time() - SCAN_LOCK.stat().st_mtime
        if age < 600:
            raise ValueError("Скан уже выполняется — подождите завершения")
        SCAN_LOCK.unlink(missing_ok=True)

    SCAN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    SCAN_LOCK.write_text(str(int(time.time())), encoding="utf-8")
    py = BASE / ".venv" / "bin" / "python3"
    env = os.environ.copy()
    env["FT_BASE"] = str(BASE)
    env.setdefault("FT_ENV", "/home/freqtrade/.freqtrade.env")

    try:
        proc = subprocess.run(
            [str(py), str(SCAN_SCRIPT), "-v"],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "scan failed").strip()[-800:]
            raise RuntimeError(err or "scan failed")
        summary: dict[str, Any] = {}
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    summary = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        return {**get_ranging_scan_status(), "ok": True, "summary": summary}
    finally:
        SCAN_LOCK.unlink(missing_ok=True)


def get_strategy_scan_status() -> dict[str, Any]:
    running = STRATEGY_SCAN_LOCK.is_file()
    if running and time.time() - STRATEGY_SCAN_LOCK.stat().st_mtime > 600:
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)
        running = False
    if not STRATEGY_PAIRS_FILE.is_file():
        return {
            "scanned_at": None,
            "whitelist": [],
            "suitable_found": 0,
            "selected_count": 0,
            "candidates_checked": 0,
            "pairs": [],
            "enabled_strategies": [],
            "by_strategy": {},
            "running": running,
        }
    data = json.loads(STRATEGY_PAIRS_FILE.read_text(encoding="utf-8"))
    return {
        "scanned_at": data.get("scanned_at"),
        "whitelist": data.get("whitelist", []),
        "suitable_found": data.get("suitable_found", 0),
        "selected_count": data.get("selected_count", 0),
        "candidates_checked": data.get("candidates_checked", 0),
        "pairs": data.get("pairs", []),
        "enabled_strategies": data.get("enabled_strategies", []),
        "pairs_per_strategy": data.get("pairs_per_strategy"),
        "by_strategy": data.get("by_strategy", {}),
        "running": running,
    }


def trigger_strategy_scan() -> dict[str, Any]:
    if STRATEGY_SCAN_LOCK.is_file():
        age = time.time() - STRATEGY_SCAN_LOCK.stat().st_mtime
        if age < 600:
            raise ValueError("Скан уже выполняется — подождите завершения")
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)

    STRATEGY_SCAN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_SCAN_LOCK.write_text(str(int(time.time())), encoding="utf-8")
    py = BASE / ".venv" / "bin" / "python3"
    env = os.environ.copy()
    env["FT_BASE"] = str(BASE)
    env.setdefault("FT_ENV", "/home/freqtrade/.freqtrade.env")

    try:
        proc = subprocess.run(
            [str(py), str(STRATEGY_SCAN_SCRIPT), "-v"],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "scan failed").strip()[-800:]
            raise RuntimeError(err or "scan failed")
        summary: dict[str, Any] = {}
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    summary = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        return {**get_strategy_scan_status(), "ok": True, "summary": summary}
    finally:
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401
        return

    def _auth_ok(self) -> bool:
        if not AUTH_PASS:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        import base64

        try:
            user, pwd = base64.b64decode(header[6:]).decode().split(":", 1)
        except Exception:
            return False
        return secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pwd, AUTH_PASS)

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="pair-config"')
            self.end_headers()
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path == "/system":
            self._json(200, get_system_stats())
            return
        if path == "/pairs":
            self._json(200, {b: safe_get_state(b) for b in CONFIGS})
            return
        if path == "/strategies":
            self._json(200, get_strategies_payload())
            return
        if path == "/logs":
            qs = parse_qs(urlparse(self.path).query)
            bots = _resolve_log_bots(qs.get("bots", ["all"])[0])
            tail = int(qs.get("tail", ["0"])[0] or 0)
            since_raw = qs.get("since", [None])[0]
            since_map: dict[str, int] | None = None
            if since_raw:
                try:
                    since_map = {k: int(v) for k, v in json.loads(since_raw).items()}
                except (json.JSONDecodeError, TypeError, ValueError):
                    since_map = None
            self._json(200, fetch_logs(bots, tail=tail, since=since_map))
            return
        if path == "/ranging-scan":
            self._json(200, get_ranging_scan_status())
            return
        if path == "/strategy-scan":
            self._json(200, get_strategy_scan_status())
            return
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "pairs" and parts[2] in CONFIGS:
            self._json(200, safe_get_state(parts[2]))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="pair-config"')
            self.end_headers()
            return
        data = self._read_json()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/ranging-scan":
            try:
                self._json(200, trigger_ranging_scan())
            except ValueError as exc:
                self._json(409, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/strategy-scan":
            try:
                self._json(200, trigger_strategy_scan())
            except ValueError as exc:
                self._json(409, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(500, {"error": str(exc)})
            return
        action = data.get("action")
        pair = data.get("pair", "")
        try:
            if action == "add":
                self._json(200, add_pair(pair))
            elif action == "remove":
                self._json(200, remove_pair(pair))
            elif action == "set_max_trades":
                bot = data.get("bot", "")
                value = data.get("max_open_trades")
                if value is None:
                    self._json(400, {"error": "max_open_trades required"})
                    return
                self._json(200, set_max_open_trades(bot, int(value)))
            elif action == "set_strategy":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                self._json(200, set_strategy(strategy_id))
            elif action == "toggle_strategy":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "enabled" not in data:
                    self._json(400, {"error": "enabled required"})
                    return
                self._json(200, toggle_strategy(strategy_id, bool(data["enabled"])))
            else:
                self._json(
                    400,
                    {
                        "error": (
                            "action must be add, remove, set_max_trades, "
                            "set_strategy, or toggle_strategy"
                        )
                    },
                )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            self._json(502, {"error": exc.read().decode()[:500]})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})


def main() -> None:
    env_file = Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    global AUTH_USER, AUTH_PASS
    AUTH_USER = os.environ.get("FREQUI_USERNAME", AUTH_USER)
    AUTH_PASS = os.environ.get("FREQUI_PASSWORD", AUTH_PASS)
    apply_bot_limits_to_configs()
    apply_strategies_prefs()
    ensure_router_config()

    host = os.environ.get("PAIR_CONFIG_HOST", "127.0.0.1")
    port = int(os.environ.get("PAIR_CONFIG_PORT", "8090"))
    ThreadedHTTPServer((host, port), Handler).serve_forever()


def apply_limits_cli() -> None:
    """Restore max_open_trades from bot_limits.json into config files."""
    env_file = Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    limits = apply_bot_limits_to_configs()
    print(json.dumps(limits, ensure_ascii=False))


def ensure_limits_cli() -> None:
    """Snapshot max_open_trades from live configs into bot_limits.json (before deploy)."""
    env_file = Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    limits = snapshot_limits_from_configs()
    print(json.dumps(limits, ensure_ascii=False))


def snapshot_strategies_cli() -> None:
    env_file = Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    enabled = snapshot_strategies_from_file()
    print(json.dumps(enabled, ensure_ascii=False))


def apply_strategies_cli() -> None:
    env_file = Path(os.environ.get("FT_ENV", "/home/freqtrade/.freqtrade.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    enabled = apply_strategies_prefs()
    print(json.dumps(enabled, ensure_ascii=False))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "apply-limits":
        apply_limits_cli()
    elif len(sys.argv) > 1 and sys.argv[1] == "ensure-limits":
        ensure_limits_cli()
    elif len(sys.argv) > 1 and sys.argv[1] == "snapshot-strategies":
        snapshot_strategies_cli()
    elif len(sys.argv) > 1 and sys.argv[1] == "apply-strategies":
        apply_strategies_cli()
    else:
        main()
