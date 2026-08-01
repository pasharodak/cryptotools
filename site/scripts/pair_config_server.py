#!/usr/bin/env python3
"""Lightweight pair whitelist admin for both Freqtrade bots."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from bybit_grid_manager import (
    close_grid,
    create_grid,
    get_history_payload,
    get_status_payload,
    load_config as load_bybit_grid_config,
    suggest_params,
    sync_all_bots_from_bybit,
    update_config as update_bybit_grid_config,
    validate_grid,
)
from scan_bybit_grid import deploy_best, get_scan_status, run_scan_only
from grid_changelog import (
    get_changelog_payload,
    record_max_open_trades,
    record_stake_amount,
    record_strategy_risk,
    record_pair_whitelist_change,
    record_ranging_scan_summary,
    record_strategy_scan_summary,
    record_strategy_toggle,
)
from adaptive_scan_scheduler import get_adaptive_scan_status, start_adaptive_scan_scheduler
from reconcile_positions import (
    archive_stale_trade,
    fix_reconcile,
    reconcile as reconcile_positions,
)

BASE = Path(os.environ.get("FT_BASE", "/home/freqtrade/freqtrade"))
FINDER_BOT_CFG = BASE / "user_data" / "finder_bot.json"

_LOG_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?(?: UTC)?)"
)
_server_log = logging.getLogger("pair_config")


def _parse_log_ts(line: str) -> str | None:
    m = _LOG_TS_RE.match(line.strip())
    return m.group(1) if m else None


def _setup_server_logging() -> None:
    log_dir = BASE / "user_data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pair-config.log"
    fmt = logging.Formatter(
        "%(asctime)s UTC - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    _server_log.setLevel(logging.INFO)
    if not _server_log.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        _server_log.addHandler(fh)


STRATEGIES_DIR = BASE / "user_data" / "strategies"
ENABLED_STRATEGIES_FILE = BASE / "user_data" / "enabled_strategies.json"
DUAL_HEDGE_FILE = BASE / "user_data" / "dual_hedge.json"
BOT_LIMITS_FILE = BASE / "user_data" / "bot_limits.json"
BOT_STRATEGIES_FILE = BASE / "user_data" / "bot_strategies.json"
DEFAULT_BOT_LIMITS = {"finder": 3, "strategy": 2, "grid": 2}
# Legacy API id (old UI used "freqai" for the Finder bot)
BOT_ALIASES = {"freqai": "finder"}


def resolve_bot(bot: str | None) -> str:
    """Normalize bot id; map legacy names to current keys."""
    name = (bot or "").strip().lower()
    return BOT_ALIASES.get(name, name)


ROUTER_STRATEGY = "MultiStrategyRouter"
CONFIGS = {
    "finder": BASE / "user_data" / "config.json",
    "strategy": BASE / "user_data" / "config_strategy.json",
    "grid": BASE / "user_data" / "config_grid.json",
}
RELOAD = {
    "finder": "http://127.0.0.1:8080/api/v1/reload_config",
    "strategy": "http://127.0.0.1:8081/api/v1/reload_config",
    "grid": "http://127.0.0.1:8082/api/v1/reload_config",
}
BLACKLIST = {
    "finder": "http://127.0.0.1:8080/api/v1/blacklist",
    "strategy": "http://127.0.0.1:8081/api/v1/blacklist",
    "grid": "http://127.0.0.1:8082/api/v1/blacklist",
}
WHITELIST = {
    "finder": "http://127.0.0.1:8080/api/v1/whitelist",
    "strategy": "http://127.0.0.1:8081/api/v1/whitelist",
    "grid": "http://127.0.0.1:8082/api/v1/whitelist",
}

AUTH_USER = os.environ.get("FREQUI_USERNAME", "freqtrader")
AUTH_PASS = os.environ.get("FREQUI_PASSWORD", "")

DEFAULT_GRID_STAKE = 10
DEFAULT_STAKES = {"grid": DEFAULT_GRID_STAKE, "strategy": 5, "finder": 5}
STAKE_EDITABLE_BOTS = frozenset({"grid", "strategy"})
DEFAULT_STRATEGY_STOPLOSS = -0.15
DEFAULT_STRATEGY_TAKE_PROFIT = 0.05
MIN_STRATEGY_STOPLOSS = -0.20
MAX_STRATEGY_STOPLOSS = -0.01
MIN_STRATEGY_TAKE_PROFIT = 0.02
MAX_STRATEGY_TAKE_PROFIT = 0.50
MIN_MAX_TRADES = 1
MAX_MAX_TRADES = 20
MIN_STAKE_AMOUNT = 1
MAX_STAKE_AMOUNT = 100

AVAILABLE_STRATEGIES = [
    {
        "id": "CriptoPairsStrategy",
        "name": "RSI + EMA + Bollinger",
        "desc": (
            "Консервативная стратегия на откатах. "
            "Лонг: RSI ниже 35, быстрая EMA выше медленной, цена у нижней полосы Bollinger. "
            "Шорт: RSI выше 65, EMA направлена вниз, цена у верхней полосы. "
            "Условия строгие — сделки бывают редко. Опционально, без отдельной ML-модели."
        ),
    },
    {
        "id": "SupertrendStrategy",
        "name": "Supertrend (тренд по ATR) (ML Gate)",
        "desc": (
            "Следует за индикатором Supertrend. "
            "Лонг при смене тренда вверх, шорт при смене вниз; RSI отсекает слабые сигналы. "
            "Хорошо подходит для выраженных трендовых движений на 5m. "
            "Своя ML-модель (sim: trend_supertrend) · profit ≥60%."
        ),
    },
    {
        "id": "MacdEmaStrategy",
        "name": "MACD + EMA 200 (ML Gate)",
        "desc": (
            "Классическое сочетание: пересечение линий MACD в сторону долгосрочного тренда. "
            "Лонг — MACD вверх и цена выше EMA 200; шорт — MACD вниз и цена ниже EMA 200. "
            "Своя ML-модель (sim: trend_macd_ema) · profit ≥60%."
        ),
    },
    {
        "id": "FibPullbackStrategy",
        "name": "Fib pullback (DCA) (ML Gate)",
        "desc": (
            "Откат к зоне Fib 0.618–0.786 по тренду 4H, DCA до 2 доборов. "
            "Своя ML-модель (sim: trend_fib) · profit ≥60%."
        ),
    },
    {
        "id": "TripleEmaStrategy",
        "name": "EMA trend (sim: trend_ema)",
        "desc": (
            "Golden cross 4H · retest EMA50 · ADX>22. Sim ML +43 USDT. "
            "ML gate profit ≥60% на входе."
        ),
    },
    {
        "id": "BollingerRsiStrategy",
        "name": "Mean-reversion BB (sim: lite_mean_rev)",
        "desc": (
            "Отбой от полос Bollinger + RSI. Sim ML +11 USDT. "
            "Работает во флэте; router блокирует вход при ADX ≥ 25. ML gate profit ≥60%."
        ),
    },
    {
        "id": "AdxMomentumStrategy",
        "name": "Breakout / ADX (sim: trend_breakout)",
        "desc": (
            "Пробой диапазона · ретест · объём. Sim ML +52 USDT. "
            "Трендовая стратегия — без ADX-cap роутера. ML gate profit ≥60%."
        ),
    },
    {
        "id": "LiteIntradayStrategy",
        "name": "Внутридневная (sim: lite_intraday)",
        "desc": (
            "MACD + узкий ADX 17–21, объём. Sim ML +11 USDT. "
            "Cooldown 6 ч после SL на паре. ML gate profit ≥60%."
        ),
    },
    {
        "id": "LiteRangeStrategy",
        "name": "Диапазонная (sim: lite_range)",
        "desc": (
            "BB bounce в боковике (ADX < 18). Sim ML +8 USDT. "
            "Cooldown 6 ч после SL. ML gate profit ≥60%."
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


def _bot_db_path(bot: str) -> Path | None:
    cfg_path = CONFIGS.get(bot)
    if not cfg_path or not cfg_path.is_file():
        return None
    db_url = load_config(cfg_path).get("db_url", "")
    if not db_url.startswith("sqlite:///"):
        return None
    name = db_url.replace("sqlite:///", "")
    for candidate in ((BASE / name), (BASE / "user_data" / name)):
        db_path = candidate.resolve()
        if db_path.is_file():
            return db_path
    return None


def load_trade_ml_meta(bot: str, trade_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Read ml_confidence / ml_gate_confidence from trade_custom_data (sqlite)."""
    if bot not in CONFIGS or not trade_ids:
        return {}
    db_path = _bot_db_path(bot)
    if not db_path:
        return {}
    ids = sorted({int(i) for i in trade_ids if int(i) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    query = f"""
        SELECT ft_trade_id, cd_key, cd_value, cd_type
        FROM trade_custom_data
        WHERE ft_trade_id IN ({placeholders})
          AND cd_key IN ('ml_confidence', 'ml_gate_confidence', 'ml_predicted')
    """
    out: dict[int, dict[str, Any]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for trade_id, key, value, cd_type in conn.execute(query, ids):
                entry = out.setdefault(int(trade_id), {})
                if cd_type == "float":
                    entry[key] = float(value)
                elif cd_type == "int":
                    entry[key] = int(value)
                elif cd_type == "bool":
                    entry[key] = value.lower() == "true"
                else:
                    entry[key] = value
    except sqlite3.Error as exc:
        _server_log.warning("trade ml meta %s: %s", bot, exc)
    return out


def get_trade_ml_meta_payload(bot: str, trade_ids: list[int]) -> dict[str, Any]:
    meta = load_trade_ml_meta(bot, trade_ids)
    return {"bot": bot, "meta": {str(k): v for k, v in meta.items()}}


def _closed_trade_row_to_json(row: sqlite3.Row, ml: dict[str, Any]) -> dict[str, Any]:
    close_profit = row["close_profit"]
    close_pct = round(float(close_profit) * 100, 2) if close_profit is not None else None
    trade: dict[str, Any] = {
        "trade_id": int(row["id"]),
        "pair": row["pair"],
        "strategy": row["strategy"],
        "enter_tag": row["enter_tag"],
        "is_open": False,
        "is_short": bool(row["is_short"]),
        "open_date": row["open_date"],
        "close_date": row["close_date"],
        "open_rate": row["open_rate"],
        "close_rate": row["close_rate"],
        "stake_amount": row["stake_amount"],
        "close_profit": close_profit,
        "close_profit_pct": close_pct,
        "close_profit_abs": row["close_profit_abs"],
        "profit_abs": row["close_profit_abs"],
        "profit_pct": close_pct,
        "realized_profit": row["realized_profit"],
        "realized_profit_ratio": close_profit,
        "exit_reason": row["exit_reason"],
    }
    if ml:
        trade["ml_meta"] = ml
    return trade


def load_closed_trades_from_db(bot: str, limit: int = 500) -> list[dict[str, Any]]:
    """Fast closed-trade list from sqlite (no Freqtrade RPC / orders payload)."""
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        return []
    db_path = _bot_db_path(bot)
    if not db_path:
        return []
    limit = max(1, min(int(limit), 5000))
    query = """
        SELECT id, pair, strategy, enter_tag, is_short,
               open_date, close_date, open_rate, close_rate,
               stake_amount, close_profit, close_profit_abs,
               exit_reason, realized_profit
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
        ORDER BY close_date DESC
        LIMIT ?
    """
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, (limit,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        meta_by_id = load_trade_ml_meta(bot, ids)
        return [
            _closed_trade_row_to_json(r, meta_by_id.get(int(r["id"]), {})) for r in rows
        ]
    except sqlite3.Error as exc:
        _server_log.warning("closed trades %s: %s", bot, exc)
        return []


def get_closed_trades_payload(limit: int = 500, bot: str | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit), 5000))
    if bot:
        bot = resolve_bot(bot)
        if bot not in CONFIGS:
            return {"error": "invalid bot"}
        return {"bot": bot, "trades": load_closed_trades_from_db(bot, limit)}
    return {name: load_closed_trades_from_db(name, limit) for name in CONFIGS}


def _default_bot_limits_payload() -> dict[str, Any]:
    return {
        "max_open_trades": dict(DEFAULT_BOT_LIMITS),
        "stake_amount": dict(DEFAULT_STAKES),
        "strategy_risk": {
            "stoploss": DEFAULT_STRATEGY_STOPLOSS,
            "take_profit": DEFAULT_STRATEGY_TAKE_PROFIT,
        },
    }


def _strategy_risk_from_config(cfg: dict[str, Any]) -> dict[str, float]:
    stoploss = float(cfg.get("stoploss", DEFAULT_STRATEGY_STOPLOSS))
    roi = cfg.get("minimal_roi") or {}
    take_profit = float(roi.get("0", DEFAULT_STRATEGY_TAKE_PROFIT))
    return {"stoploss": stoploss, "take_profit": take_profit}


def load_strategy_risk() -> dict[str, float]:
    """Read strategy bot SL/TP from config (with bot_limits.json fallback)."""
    cfg_path = CONFIGS.get("strategy")
    if cfg_path and cfg_path.is_file():
        risk = _strategy_risk_from_config(load_config(cfg_path))
    else:
        risk = {
            "stoploss": DEFAULT_STRATEGY_STOPLOSS,
            "take_profit": DEFAULT_STRATEGY_TAKE_PROFIT,
        }
    if BOT_LIMITS_FILE.is_file():
        stored = json.loads(BOT_LIMITS_FILE.read_text(encoding="utf-8")).get("strategy_risk", {})
        if "stoploss" in stored:
            risk["stoploss"] = float(stored["stoploss"])
        if "take_profit" in stored:
            risk["take_profit"] = float(stored["take_profit"])
    return risk


def strategy_risk_payload(risk: dict[str, float] | None = None) -> dict[str, Any]:
    data = dict(risk or load_strategy_risk())
    sl = float(data["stoploss"])
    tp = float(data["take_profit"])
    return {
        "stoploss": sl,
        "take_profit": tp,
        "stoploss_pct": round(abs(sl) * 100, 2),
        "take_profit_pct": round(tp * 100, 2),
    }


def save_strategy_risk_to_limits(risk: dict[str, float]) -> None:
    payload = load_bot_limits_file() if BOT_LIMITS_FILE.is_file() else _default_bot_limits_payload()
    payload["strategy_risk"] = {
        "stoploss": float(risk["stoploss"]),
        "take_profit": float(risk["take_profit"]),
    }
    BOT_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_LIMITS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(BOT_LIMITS_FILE)


def apply_strategy_risk_to_config(risk: dict[str, float]) -> None:
    path = CONFIGS["strategy"]
    cfg = load_config(path)
    sl = float(risk["stoploss"])
    tp = float(risk["take_profit"])
    changed = False
    if float(cfg.get("stoploss", 0)) != sl:
        cfg["stoploss"] = sl
        changed = True
    roi = dict(cfg.get("minimal_roi") or {})
    if float(roi.get("0", 0)) != tp:
        roi["0"] = tp
        cfg["minimal_roi"] = roi
        changed = True
    if changed:
        save_config(path, cfg)


def set_strategy_risk(stoploss_pct: float, take_profit_pct: float) -> dict[str, Any]:
    """Set strategy bot stoploss / take-profit (percent inputs, e.g. 5 → −5%)."""
    sl_pct = float(stoploss_pct)
    tp_pct = float(take_profit_pct)
    if sl_pct < abs(MIN_STRATEGY_STOPLOSS) * 100 or sl_pct > abs(MAX_STRATEGY_STOPLOSS) * 100:
        raise ValueError(
            f"stoploss must be between {abs(MIN_STRATEGY_STOPLOSS) * 100:g}% "
            f"and {abs(MAX_STRATEGY_STOPLOSS) * 100:g}%"
        )
    if tp_pct < MIN_STRATEGY_TAKE_PROFIT * 100 or tp_pct > MAX_STRATEGY_TAKE_PROFIT * 100:
        raise ValueError(
            f"take_profit must be between {MIN_STRATEGY_TAKE_PROFIT * 100:g}% "
            f"and {MAX_STRATEGY_TAKE_PROFIT * 100:g}%"
        )
    old = load_strategy_risk()
    risk = {"stoploss": -abs(sl_pct) / 100.0, "take_profit": abs(tp_pct) / 100.0}
    apply_strategy_risk_to_config(risk)
    save_strategy_risk_to_limits(risk)
    reload_result: Any = None
    reload_warning: str | None = None
    try:
        reload_result = reload_bot("strategy")
    except BaseException as exc:  # noqa: BLE001
        reload_warning = str(exc)
    result: dict[str, Any] = {
        "risk": strategy_risk_payload(risk),
        "reloaded": reload_result,
        "state": safe_get_state("strategy"),
    }
    if reload_warning:
        result["reload_warning"] = reload_warning
    if old != risk:
        try:
            record_strategy_risk(old, risk)
        except Exception:
            pass
    return result


def load_bot_limits_file() -> dict[str, Any]:
    """Read persistent bot limits (max trades + grid stake)."""
    if not BOT_LIMITS_FILE.is_file():
        return ensure_bot_limits_snapshot()
    data = json.loads(BOT_LIMITS_FILE.read_text(encoding="utf-8"))
    payload = _default_bot_limits_payload()
    stored_trades = data.get("max_open_trades", data if "stake_amount" not in data else {})
    # Migrate legacy bot id
    if "freqai" in stored_trades and "finder" not in stored_trades:
        stored_trades = dict(stored_trades)
        stored_trades["finder"] = stored_trades.pop("freqai")
    for bot in CONFIGS:
        if bot in stored_trades:
            payload["max_open_trades"][bot] = int(stored_trades[bot])
    stored_stakes = data.get("stake_amount", {})
    if "freqai" in stored_stakes and "finder" not in stored_stakes:
        stored_stakes = dict(stored_stakes)
        stored_stakes["finder"] = stored_stakes.pop("freqai")
    for bot, default in DEFAULT_STAKES.items():
        if bot in stored_stakes:
            payload["stake_amount"][bot] = float(stored_stakes[bot])
        else:
            payload["stake_amount"][bot] = float(default)
    return payload


def load_bot_limits() -> dict[str, int]:
    """Read persistent max_open_trades per bot."""
    payload = load_bot_limits_file()
    return {bot: int(payload["max_open_trades"][bot]) for bot in CONFIGS}


def load_grid_stake() -> float:
    return float(load_bot_limits_file()["stake_amount"]["grid"])


def load_bot_stake(bot: str) -> float:
    return float(load_bot_limits_file()["stake_amount"].get(bot, DEFAULT_STAKES.get(bot, 5)))


def snapshot_limits_from_configs() -> dict[str, int]:
    """Read max_open_trades from live config files and persist to bot_limits.json."""
    limits: dict[str, int] = {}
    stakes: dict[str, float] = dict(DEFAULT_STAKES)
    for bot, path in CONFIGS.items():
        try:
            cfg = load_config(path)
            raw = cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2))
            if raw == float("inf"):
                raw = MAX_MAX_TRADES
            limits[bot] = int(raw)
            stakes[bot] = float(cfg.get("stake_amount", DEFAULT_STAKES.get(bot, 5)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            limits[bot] = DEFAULT_BOT_LIMITS.get(bot, 2)
    save_bot_limits(limits, stakes=stakes)
    return limits


def ensure_bot_limits_snapshot() -> dict[str, int]:
    """Create bot_limits.json from configs only when missing."""
    if BOT_LIMITS_FILE.is_file():
        return load_bot_limits()
    return snapshot_limits_from_configs()


def save_bot_limits(
    limits: dict[str, int],
    *,
    grid_stake: float | None = None,
    stakes: dict[str, float] | None = None,
) -> None:
    BOT_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_LIMITS_FILE.with_suffix(".json.tmp")
    payload = load_bot_limits_file() if BOT_LIMITS_FILE.is_file() else _default_bot_limits_payload()
    payload["max_open_trades"] = {bot: int(limits[bot]) for bot in CONFIGS if bot in limits}
    if stakes:
        for bot, value in stakes.items():
            if bot in DEFAULT_STAKES:
                payload["stake_amount"][bot] = float(value)
    if grid_stake is not None:
        payload["stake_amount"]["grid"] = float(grid_stake)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(BOT_LIMITS_FILE)


def apply_bot_limits_to_configs() -> dict[str, int]:
    """Write saved limits into all bot config files."""
    payload = load_bot_limits_file()
    limits = {bot: int(payload["max_open_trades"][bot]) for bot in CONFIGS}
    stakes = payload.get("stake_amount", {})
    for bot, path in CONFIGS.items():
        if bot not in limits or not path.is_file():
            continue
        cfg = load_config(path)
        changed = False
        value = int(limits[bot])
        if cfg.get("max_open_trades") != value:
            cfg["max_open_trades"] = value
            changed = True
        if bot in stakes:
            stake_val = float(stakes[bot])
            if float(cfg.get("stake_amount", 0)) != stake_val:
                cfg["stake_amount"] = stake_val
                changed = True
        if changed:
            save_config(path, cfg)
    risk = load_strategy_risk()
    apply_strategy_risk_to_config(risk)
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


def load_finder_bot_config() -> dict[str, Any]:
    if not FINDER_BOT_CFG.is_file():
        return {"enabled": True}
    try:
        return json.loads(FINDER_BOT_CFG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": True}


def is_finder_bot_enabled() -> bool:
    return bool(load_finder_bot_config().get("enabled", True))


def reload_bot(bot: str) -> Any:
    return api_call(
        RELOAD[bot],
        "POST",
        retries=RELOAD_RETRIES,
        retry_delay=RELOAD_RETRY_DELAY,
    )


def safe_get_state(bot: str) -> dict[str, Any]:
    bot = resolve_bot(bot)
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


PROD_DEFAULT_STRATEGIES = frozenset({
    "BollingerRsiStrategy",
    "AdxMomentumStrategy",
    "LiteRangeStrategy",
    "SupertrendStrategy",
    "MacdEmaStrategy",
    "FibPullbackStrategy",
})


def default_enabled_map() -> dict[str, bool]:
    return {s["id"]: s["id"] in PROD_DEFAULT_STRATEGIES for s in AVAILABLE_STRATEGIES}


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


def load_inverted_map() -> dict[str, bool]:
    result = {s["id"]: False for s in AVAILABLE_STRATEGIES}
    if not ENABLED_STRATEGIES_FILE.is_file():
        return result
    try:
        data = json.loads(ENABLED_STRATEGIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    inverted = data.get("inverted", {})
    for sid in result:
        if sid in inverted:
            result[sid] = bool(inverted[sid])
    return result


def _read_enabled_file() -> dict[str, Any]:
    if not ENABLED_STRATEGIES_FILE.is_file():
        return {}
    try:
        return json.loads(ENABLED_STRATEGIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_enabled_map(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
) -> None:
    ENABLED_STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _read_enabled_file()
    data["enabled"] = enabled
    if inverted is not None:
        data["inverted"] = {sid: bool(inverted.get(sid, False)) for sid in enabled}
    else:
        prev = data.get("inverted", {})
        data["inverted"] = {sid: bool(prev.get(sid, False)) for sid in enabled}
    tmp = ENABLED_STRATEGIES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(ENABLED_STRATEGIES_FILE)
    save_strategies_prefs(enabled, inverted=data["inverted"])


def save_strategies_prefs(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
) -> None:
    BOT_STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"enabled": enabled}
    if inverted is not None:
        payload["inverted"] = inverted
    elif BOT_STRATEGIES_FILE.is_file():
        try:
            old = json.loads(BOT_STRATEGIES_FILE.read_text(encoding="utf-8"))
            if "inverted" in old:
                payload["inverted"] = old["inverted"]
        except (json.JSONDecodeError, OSError):
            pass
    tmp = BOT_STRATEGIES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
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
    inverted = load_inverted_map()
    stored_inv = data.get("inverted", {})
    for sid in inverted:
        if sid in stored_inv:
            inverted[sid] = bool(stored_inv[sid])
    save_enabled_map(result, inverted=inverted)
    return result


def ensure_router_config() -> None:
    cfg = load_config(CONFIGS["strategy"])
    if cfg.get("strategy") != ROUTER_STRATEGY:
        cfg["strategy"] = ROUTER_STRATEGY
        save_config(CONFIGS["strategy"], cfg)


def load_dual_hedge_enabled() -> bool:
    if not DUAL_HEDGE_FILE.is_file():
        return False
    try:
        data = json.loads(DUAL_HEDGE_FILE.read_text(encoding="utf-8"))
        return bool(data.get("enabled", False))
    except (json.JSONDecodeError, OSError):
        return False


def _save_dual_hedge_file(enabled: bool) -> None:
    DUAL_HEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "note": (
            "При включении на каждый сигнал открываются две позиции: по направлению сигнала "
            "и противоположная (хедж). Требует hedge mode на Bybit."
        ),
    }
    tmp = DUAL_HEDGE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(DUAL_HEDGE_FILE)


def _apply_hedge_mode_to_configs(enabled: bool) -> None:
    """Sync hedge_mode flag so Bybit stays in hedge/one-way mode consistently."""
    for bot in ("strategy", "grid"):
        path = CONFIGS.get(bot)
        if not path or not path.is_file():
            continue
        cfg = load_config(path)
        if bool(cfg.get("hedge_mode", False)) == bool(enabled):
            continue
        cfg["hedge_mode"] = bool(enabled)
        save_config(path, cfg)


def set_dual_hedge(enabled: bool) -> dict[str, Any]:
    enabled = bool(enabled)
    _save_dual_hedge_file(enabled)
    _apply_hedge_mode_to_configs(enabled)
    # Dual L+S needs at least 2 slots per pair — bump strategy max if too low.
    limits_note = None
    if enabled:
        cfg = load_config(CONFIGS["strategy"])
        mot = int(cfg.get("max_open_trades") or 0)
        if mot < 4:
            next_mot = max(mot, 4)
            if next_mot != mot:
                cfg["max_open_trades"] = next_mot
                save_config(CONFIGS["strategy"], cfg)
                limits = load_bot_limits()
                limits["strategy"] = next_mot
                save_bot_limits(limits)
                limits_note = (
                    f"max_open_trades strategy raised to {next_mot} "
                    "(need 2 slots per dual pair)"
                )
    reload_result: Any = None
    reload_warning: str | None = None
    try:
        reload_result = reload_bot("strategy")
    except BaseException as exc:  # noqa: BLE001
        reload_warning = str(exc)
    result: dict[str, Any] = {
        "dual_hedge": enabled,
        "reloaded": reload_result,
        **get_strategies_payload(),
    }
    if limits_note:
        result["limits_note"] = limits_note
    if reload_warning:
        result["reload_warning"] = reload_warning
    return result


def get_strategies_payload() -> dict[str, Any]:
    enabled = load_enabled_map()
    enabled_ids = [sid for sid, on in enabled.items() if on]
    dual_hedge = load_dual_hedge_enabled()
    inverted = load_inverted_map()
    return {
        "router": ROUTER_STRATEGY,
        "enabled": enabled,
        "enabled_ids": enabled_ids,
        "enabled_count": len(enabled_ids),
        "strategies": get_strategy_catalog(),
        "risk": strategy_risk_payload(),
        "dual_hedge": dual_hedge,
        "inverted": inverted,
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
    try:
        record_strategy_toggle(strategy_id, bool(enabled))
    except Exception:
        pass
    # Router reads enabled_strategies.json on each signal — reload would restart the bot.
    return {
        "strategy": strategy_id,
        "enabled": state[strategy_id],
        **get_strategies_payload(),
    }


def toggle_strategy_invert(strategy_id: str, inverted: bool) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    enabled = load_enabled_map()
    state = load_inverted_map()
    state[strategy_id] = bool(inverted)
    save_enabled_map(enabled, inverted=state)
    # Router reads inverted flags from enabled_strategies.json each candle.
    return {
        "strategy": strategy_id,
        "inverted": state[strategy_id],
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
        "stake_amount": float(cfg.get("stake_amount", DEFAULT_GRID_STAKE if bot == "grid" else 5)),
    }
    if bot == "strategy":
        enabled = load_enabled_map()
        state["strategy"] = ROUTER_STRATEGY
        state["enabled_strategies"] = enabled
        state["enabled_count"] = sum(1 for on in enabled.values() if on)
        state["risk"] = strategy_risk_payload()
        state["dual_hedge"] = load_dual_hedge_enabled()
        state["inverted"] = load_inverted_map()
    return state


def sync_finder_config(mutator) -> list[str]:
    """Manual pair changes apply to ML Finder only (strategy/grid use scanners)."""
    path = CONFIGS["finder"]
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

    sync_finder_config(add)
    try:
        record_pair_whitelist_change("add", pair)
    except Exception:
        pass
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
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        raise ValueError("bot must be finder, strategy, or grid")
    value = int(value)
    if value < MIN_MAX_TRADES or value > MAX_MAX_TRADES:
        raise ValueError(
            f"max_open_trades must be between {MIN_MAX_TRADES} and {MAX_MAX_TRADES}"
        )

    cfg = load_config(CONFIGS[bot])
    old_val = int(cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2)))
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
    if old_val != value:
        try:
            record_max_open_trades(bot, old_val, value)
        except Exception:
            pass
    return result


def set_stake_amount(bot: str, value: float) -> dict[str, Any]:
    bot = resolve_bot(bot)
    if bot not in STAKE_EDITABLE_BOTS:
        allowed = ", ".join(sorted(STAKE_EDITABLE_BOTS))
        raise ValueError(f"stake_amount can only be changed for: {allowed}")
    value = float(value)
    if value < MIN_STAKE_AMOUNT or value > MAX_STAKE_AMOUNT:
        raise ValueError(
            f"stake_amount must be between {MIN_STAKE_AMOUNT} and {MAX_STAKE_AMOUNT}"
        )

    cfg = load_config(CONFIGS[bot])
    default_stake = DEFAULT_STAKES.get(bot, 5)
    old_val = float(cfg.get("stake_amount", default_stake))
    cfg["stake_amount"] = value
    save_config(CONFIGS[bot], cfg)
    limits = load_bot_limits()
    stakes = dict(load_bot_limits_file().get("stake_amount", DEFAULT_STAKES))
    stakes[bot] = value
    save_bot_limits(limits, stakes=stakes)
    reload_result: Any = None
    reload_warning: str | None = None
    try:
        reload_result = reload_bot(bot)
    except BaseException as exc:  # noqa: BLE001
        reload_warning = str(exc)
    result: dict[str, Any] = {
        "bot": bot,
        "stake_amount": value,
        "reloaded": reload_result,
        "state": safe_get_state(bot),
    }
    if reload_warning:
        result["reload_warning"] = reload_warning
    if old_val != value:
        try:
            record_stake_amount(bot, old_val, value)
        except Exception:
            pass
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

    sync_finder_config(remove)
    try:
        record_pair_whitelist_change("remove", pair)
    except Exception:
        pass
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
    "finder": ("ML Finder", BASE / "user_data" / "logs" / "freqtrade-finder.log"),
    "strategy": ("Стратегии", BASE / "user_data" / "logs" / "freqtrade-strategy.log"),
    "grid": ("Grid", BASE / "user_data" / "logs" / "freqtrade-grid.log"),
    "scanner": ("Сканер Grid", BASE / "user_data" / "logs" / "ranging-scanner.log"),
    "strategy_scanner": ("Сканер страт.", BASE / "user_data" / "logs" / "strategy-scanner.log"),
    "pair_config": ("UI API", BASE / "user_data" / "logs" / "pair-config.log"),
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
            entry: dict[str, str] = {"bot": bot, "label": label, "line": line}
            ts = _parse_log_ts(line)
            if ts:
                entry["ts"] = ts
            entries.append(entry)

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
        try:
            if summary:
                record_ranging_scan_summary(summary)
        except Exception:
            pass
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
        try:
            status = get_strategy_scan_status()
            if status.get("whitelist"):
                record_strategy_scan_summary(status)
        except Exception:
            pass
        return {**get_strategy_scan_status(), "ok": True, "summary": summary}
    finally:
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401
        _server_log.info("%s - %s", self.address_string(), fmt % args)

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
            payload = {b: safe_get_state(b) for b in CONFIGS}
            mode_path = BASE / "user_data/pairlist_mode.json"
            if mode_path.is_file():
                try:
                    payload["_pairlist_mode"] = json.loads(mode_path.read_text(encoding="utf-8")).get(
                        "mode", "scanner"
                    )
                except (json.JSONDecodeError, OSError):
                    payload["_pairlist_mode"] = "scanner"
            else:
                payload["_pairlist_mode"] = "scanner"
            payload["_finder_bot_enabled"] = is_finder_bot_enabled()
            payload["_finder_bot"] = load_finder_bot_config()
            self._json(200, payload)
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
        if path == "/changelog":
            self._json(200, get_changelog_payload())
            return
        if path == "/trade-ml-meta":
            qs = parse_qs(urlparse(self.path).query)
            bot = resolve_bot((qs.get("bot") or [""])[0])
            raw_ids = (qs.get("ids") or [""])[0]
            if bot not in CONFIGS:
                self._json(400, {"error": "invalid bot"})
                return
            trade_ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
            self._json(200, get_trade_ml_meta_payload(bot, trade_ids))
            return
        if path == "/closed-trades":
            qs = parse_qs(urlparse(self.path).query)
            raw_bot = (qs.get("bot") or [""])[0] or None
            bot = resolve_bot(raw_bot) if raw_bot else None
            try:
                limit = int((qs.get("limit") or ["500"])[0])
            except ValueError:
                limit = 500
            payload = get_closed_trades_payload(limit, bot)
            if payload.get("error"):
                self._json(400, payload)
                return
            self._json(200, payload)
            return
        if path == "/adaptive-scan":
            self._json(200, get_adaptive_scan_status(BASE))
            return
        if path == "/position-reconcile":
            try:
                self._json(200, reconcile_positions(BASE))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc), "ok": False})
            return
        if path == "/bybit-grid":
            self._json(200, get_status_payload())
            return
        if path == "/bybit-grid/history":
            self._json(200, get_history_payload())
            return
        if path == "/bybit-grid/config":
            self._json(200, load_bybit_grid_config())
            return
        if path == "/bybit-grid/suggest":
            qs = parse_qs(urlparse(self.path).query)
            pair = (qs.get("pair") or [""])[0]
            if not pair:
                self._json(400, {"error": "pair required"})
                return
            try:
                self._json(200, suggest_params(pair))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/scan":
            self._json(200, get_scan_status())
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
        if path == "/position-reconcile/fix":
            try:
                self._json(200, fix_reconcile(BASE))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc), "ok": False})
            return
        if path == "/position-reconcile/archive":
            bot = resolve_bot(data.get("bot") or "")
            trade_id = data.get("trade_id")
            if bot not in CONFIGS or trade_id is None:
                self._json(400, {"error": "bot and trade_id required"})
                return
            try:
                self._json(200, archive_stale_trade(bot, int(trade_id), BASE))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc), "ok": False})
            return
        if path == "/bybit-grid/validate":
            try:
                self._json(200, validate_grid(data))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/create":
            try:
                self._json(200, create_grid(data))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/close":
            bot_id = data.get("bot_id", "")
            if not bot_id:
                self._json(400, {"error": "bot_id required"})
                return
            try:
                self._json(200, close_grid(str(bot_id)))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/auto":
            dry_run = bool(data.get("dry_run"))
            force = bool(data.get("force"))
            try:
                self._json(200, deploy_best(dry_run=dry_run, force=force))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/scan":
            try:
                self._json(200, run_scan_only())
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/sync":
            try:
                extra = data.get("bot_ids") if isinstance(data.get("bot_ids"), list) else None
                scan = bool(data.get("scan", False))
                full = bool(data.get("full")) or scan or bool(extra)
                self._json(
                    200,
                    sync_all_bots_from_bybit(
                        extra,
                        scan_missing=scan,
                        full_reconcile=full,
                    ),
                )
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/bybit-grid/config":
            try:
                self._json(200, update_bybit_grid_config(data))
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
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
            elif action == "set_stake":
                bot = data.get("bot", "")
                value = data.get("stake_amount")
                if value is None:
                    self._json(400, {"error": "stake_amount required"})
                    return
                self._json(200, set_stake_amount(bot, float(value)))
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
            elif action == "toggle_strategy_invert":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "inverted" not in data:
                    self._json(400, {"error": "inverted required"})
                    return
                self._json(200, toggle_strategy_invert(strategy_id, bool(data["inverted"])))
            elif action == "set_dual_hedge":
                if "enabled" not in data:
                    self._json(400, {"error": "enabled required"})
                    return
                self._json(200, set_dual_hedge(bool(data["enabled"])))
            elif action == "set_strategy_risk":
                sl = data.get("stoploss_pct")
                tp = data.get("take_profit_pct")
                if sl is None or tp is None:
                    self._json(400, {"error": "stoploss_pct and take_profit_pct required"})
                    return
                try:
                    self._json(200, set_strategy_risk(float(sl), float(tp)))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            else:
                self._json(
                    400,
                    {
                        "error": (
                            "action must be add, remove, set_max_trades, set_stake, "
                            "set_strategy, toggle_strategy, toggle_strategy_invert, "
                            "set_dual_hedge, or set_strategy_risk"
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
    _setup_server_logging()
    host = os.environ.get("PAIR_CONFIG_HOST", "127.0.0.1")
    port = int(os.environ.get("PAIR_CONFIG_PORT", "8090"))
    _server_log.info("pair-config server starting on %s:%s", host, port)
    start_adaptive_scan_scheduler(
        BASE,
        ranging_scan=trigger_ranging_scan,
        strategy_scan=trigger_strategy_scan,
        poll_sec=60,
    )
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
