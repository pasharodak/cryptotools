#!/usr/bin/env python3
"""Pair whitelist / limits admin API for CryptoTools bots."""
from __future__ import annotations

import base64
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
from collections.abc import Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import tenant_manager as tm
import tenant_context as tc
from bybit_grid_manager import (
    close_grid,
    create_grid,
    get_history_payload,
    get_status_payload,
    load_config as load_bybit_grid_config,
    suggest_params,
    sync_all_bots_from_bybit,
    tenant_bybit_context,
    update_config as update_bybit_grid_config,
    validate_grid,
)
from scan_bybit_grid import deploy_best, get_scan_status, run_scan_only
from grid_changelog import (
    get_changelog_payload,
    record_max_open_trades,
    record_max_open_trades_per_strategy,
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

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
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
DEFAULT_BOT_LIMITS = {"finder": 3, "strategy": 2, "grid": 2}
# 0 = no per-strategy cap (only global max_open_trades applies)
DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY = 0


def enabled_strategies_file() -> Path:
    return tc.user_data_dir(BASE) / "enabled_strategies.json"


def dual_hedge_file() -> Path:
    return tc.user_data_dir(BASE) / "dual_hedge.json"


def max_open_trades_per_strategy_file() -> Path:
    return tc.user_data_dir(BASE) / "max_open_trades_per_strategy.json"


def bot_limits_file() -> Path:
    return tc.user_data_dir(BASE) / "bot_limits.json"


def bot_strategies_file() -> Path:
    return tc.user_data_dir(BASE) / "bot_strategies.json"


def resolve_bot(bot: str | None) -> str:
    """Normalize bot id."""
    return (bot or "").strip().lower()


ROUTER_STRATEGY = "MultiStrategyRouter"
_ADMIN_CONFIGS = {
    "finder": BASE / "user_data" / "config.json",
    "strategy": BASE / "user_data" / "config_strategy.json",
    "grid": BASE / "user_data" / "config_grid.json",
}
_ADMIN_RELOAD = {
    "finder": "http://127.0.0.1:8080/api/v1/reload_config",
    "strategy": "http://127.0.0.1:8081/api/v1/reload_config",
    "grid": "http://127.0.0.1:8082/api/v1/reload_config",
}
_ADMIN_BLACKLIST = {
    "finder": "http://127.0.0.1:8080/api/v1/blacklist",
    "strategy": "http://127.0.0.1:8081/api/v1/blacklist",
    "grid": "http://127.0.0.1:8082/api/v1/blacklist",
}
_ADMIN_WHITELIST = {
    "finder": "http://127.0.0.1:8080/api/v1/whitelist",
    "strategy": "http://127.0.0.1:8081/api/v1/whitelist",
    "grid": "http://127.0.0.1:8082/api/v1/whitelist",
}


class _ResolvedMapping(Mapping):
    """Lazy Mapping that re-resolves under the current request tenant context."""

    def __init__(self, resolver):
        self._resolver = resolver

    def _data(self) -> dict:
        return self._resolver()

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def __repr__(self) -> str:
        return repr(self._data())


CONFIGS = _ResolvedMapping(lambda: tc.resolve_configs(BASE, _ADMIN_CONFIGS))
RELOAD = _ResolvedMapping(lambda: tc.resolve_api_urls("reload_config"))
START = _ResolvedMapping(lambda: tc.resolve_api_urls("start"))
STOP = _ResolvedMapping(lambda: tc.resolve_api_urls("stop"))
BLACKLIST = _ResolvedMapping(lambda: tc.resolve_api_urls("blacklist"))
WHITELIST = _ResolvedMapping(lambda: tc.resolve_api_urls("whitelist"))

AUTH_USER = os.environ.get("FREQUI_USERNAME", "cryptotools")
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
MIN_MAX_TRADES = 0
MAX_MAX_TRADES = 50
MIN_STAKE_AMOUNT = 1
MAX_STAKE_AMOUNT = 100

AVAILABLE_STRATEGIES = [
    {
        "id": "AltVolumeBreakoutStrategy",
        "num": 31,
        "ui_order": 0,
        "name": "Alt volume breakout",
        "desc": "ML pack · sim scalp_liq_breakout · test ML PnL 135.3 USDT · SL -1.5% · TP 1.2% · gate>=55% · exp exp31_mlp",
    },
    {
        "id": "PsaraFlipStrategy",
        "num": 1,
        "ui_order": 1,
        "name": "Parabolic SAR flip",
        "desc": "ML pack · sim new_psar · test ML PnL 87.1 USDT · SL -2.0% · TP 1.4% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "AtrChannelBreakoutStrategy",
        "num": 2,
        "ui_order": 2,
        "name": "ATR channel breakout",
        "desc": "ML pack · sim chart3_atrch · test ML PnL 81.0 USDT · SL -2.0% · TP 1.4% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "CmfZeroCrossStrategy",
        "num": 3,
        "ui_order": 3,
        "name": "CMF zero cross",
        "desc": "ML pack · sim chart2_cmf · test ML PnL 79.9 USDT · SL -1.7% · TP 1.1% · gate>=55% · exp exp10_xgb_pos_weight",
    },
    {
        "id": "ScalpEmaCrossStrategy",
        "num": 4,
        "ui_order": 4,
        "name": "Scalp EMA 8/21",
        "desc": "ML pack · sim scalp_ema · test ML PnL 76.7 USDT · SL -1.0% · TP 0.8% · gate>=55% · exp exp08_lgbm_regularized",
    },
    {
        "id": "ChaikinOscStrategy",
        "num": 5,
        "ui_order": 5,
        "name": "Chaikin Oscillator",
        "desc": "ML pack · sim chart3_adosc · test ML PnL 68.2 USDT · SL -1.7% · TP 1.1% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "DonchianBreakoutStrategy",
        "num": 6,
        "ui_order": 6,
        "name": "Donchian / Turtle",
        "desc": "ML pack · sim new_donchian · test ML PnL 66.8 USDT · SL -2.5% · TP 1.8% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "PpoSignalStrategy",
        "num": 7,
        "ui_order": 7,
        "name": "PPO signal cross",
        "desc": "ML pack · sim chart3_ppo · test ML PnL 66.4 USDT · SL -1.6% · TP 1.0% · gate>=55% · exp exp22_xgb_shallow",
    },
    {
        "id": "DonchianAdxVolComboStrategy",
        "num": 8,
        "ui_order": 8,
        "name": "Donchian+ADX+Vol",
        "desc": "ML pack · sim combo_don_adx_vol · test ML PnL 66.1 USDT · SL -2.2% · TP 1.8% · gate>=65% · exp exp15_lgbm_gate065_nocal",
    },
    {
        "id": "ObvEmaCrossStrategy",
        "num": 9,
        "ui_order": 9,
        "name": "OBV EMA cross",
        "desc": "ML pack · sim chart2_obv · test ML PnL 62.3 USDT · SL -1.8% · TP 1.2% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "ElderRayStrategy",
        "num": 10,
        "ui_order": 10,
        "name": "Elder Ray Bull/Bear",
        "desc": "ML pack · sim chart3_elder · test ML PnL 62.2 USDT · SL -1.7% · TP 1.1% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "ScalpMacdHistStrategy",
        "num": 11,
        "ui_order": 11,
        "name": "Scalp MACD hist",
        "desc": "ML pack · sim scalp_macd · test ML PnL 60.5 USDT · SL -1.2% · TP 0.9% · gate>=55% · exp exp09_xgb_baseline",
    },
    {
        "id": "KeltnerBreakoutStrategy",
        "num": 12,
        "ui_order": 12,
        "name": "Keltner breakout",
        "desc": "ML pack · sim new_keltner · test ML PnL 57.4 USDT · SL -2.0% · TP 1.5% · gate>=65% · exp exp15_lgbm_gate065_nocal",
    },
    {
        "id": "HeikinAshiFlipStrategy",
        "num": 13,
        "ui_order": 13,
        "name": "Heikin Ashi flip",
        "desc": "ML pack · sim chart_ha · test ML PnL 57.2 USDT · SL -1.6% · TP 1.1% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "VortexCrossStrategy",
        "num": 14,
        "ui_order": 14,
        "name": "Vortex VI+/VI−",
        "desc": "ML pack · sim chart2_vortex · test ML PnL 57.1 USDT · SL -1.9% · TP 1.3% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "AwesomeOscStrategy",
        "num": 15,
        "ui_order": 15,
        "name": "Awesome Oscillator",
        "desc": "ML pack · sim chart3_ao · test ML PnL 56.7 USDT · SL -1.7% · TP 1.1% · gate>=55% · exp exp22_xgb_shallow",
    },
    {
        "id": "KeltnerStochVolComboStrategy",
        "num": 16,
        "ui_order": 16,
        "name": "Keltner+Stoch+Vol",
        "desc": "ML pack · sim combo_kc_stoch_vol · test ML PnL 52.9 USDT · SL -2.0% · TP 1.5% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "TemaCrossStrategy",
        "num": 17,
        "ui_order": 17,
        "name": "TEMA fast/slow",
        "desc": "ML pack · sim chart3_tema · test ML PnL 50.7 USDT · SL -1.8% · TP 1.2% · gate>=50% · exp exp29_lgbm_gate050",
    },
    {
        "id": "TrixSignalStrategy",
        "num": 18,
        "ui_order": 18,
        "name": "TRIX signal cross",
        "desc": "ML pack · sim chart2_trix · test ML PnL 49.1 USDT · SL -1.7% · TP 1.1% · gate>=55% · exp exp23_xgb_deep",
    },
    {
        "id": "RocMomentumStrategy",
        "num": 19,
        "ui_order": 19,
        "name": "ROC momentum",
        "desc": "ML pack · sim chart3_roc · test ML PnL 46.2 USDT · SL -1.6% · TP 1.0% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "HmaPpoAtrComboStrategy",
        "num": 20,
        "ui_order": 20,
        "name": "HMA+PPO+ATR",
        "desc": "ML pack · sim combo_hma_ppo_atr · test ML PnL 45.5 USDT · SL -1.8% · TP 1.4% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "WilliamsRReclaimStrategy",
        "num": 21,
        "ui_order": 21,
        "name": "Williams %R reclaim",
        "desc": "ML pack · sim chart_willr · test ML PnL 41.5 USDT · SL -1.5% · TP 1.0% · gate>=70% · exp exp30_lgbm_gate070",
    },
    {
        "id": "BbSqueezeBreakoutStrategy",
        "num": 22,
        "ui_order": 22,
        "name": "BB squeeze breakout",
        "desc": "ML pack · sim chart_squeeze · test ML PnL 41.3 USDT · SL -1.8% · TP 1.3% · gate>=55% · exp exp03_lgbm_shallow",
    },
    {
        "id": "EmaRsiAtrComboStrategy",
        "num": 23,
        "ui_order": 23,
        "name": "EMA+RSI+ATR triad",
        "desc": "ML pack · sim combo_ema_rsi_atr · test ML PnL 41.3 USDT · SL -1.8% · TP 1.4% · gate>=55% · exp exp24_xgb_slow",
    },
    {
        "id": "AroonCrossStrategy",
        "num": 24,
        "ui_order": 24,
        "name": "Aroon Up/Down cross",
        "desc": "ML pack · sim chart2_aroon · test ML PnL 39.1 USDT · SL -1.8% · TP 1.2% · gate>=55% · exp exp22_xgb_shallow",
    },
    {
        "id": "AdxMacdVolComboStrategy",
        "num": 25,
        "ui_order": 25,
        "name": "ADX+MACD+Vol",
        "desc": "ML pack · sim combo_adx_macd_vol · test ML PnL 38.0 USDT · SL -2.0% · TP 1.6% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "EngulfingTrendStrategy",
        "num": 26,
        "ui_order": 26,
        "name": "Engulfing + EMA",
        "desc": "ML pack · sim chart2_engulf · test ML PnL 36.0 USDT · SL -1.6% · TP 1.1% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "AdxDiCrossStrategy",
        "num": 27,
        "ui_order": 27,
        "name": "ADX DI+/DI− cross",
        "desc": "ML pack · sim chart_adxdi · test ML PnL 34.6 USDT · SL -2.0% · TP 1.4% · gate>=65% · exp exp15_lgbm_gate065_nocal",
    },
    {
        "id": "MfiReclaimStrategy",
        "num": 28,
        "ui_order": 28,
        "name": "MFI reclaim",
        "desc": "ML pack · sim chart2_mfi · test ML PnL 31.8 USDT · SL -1.5% · TP 1.0% · gate>=55% · exp exp03_lgbm_shallow",
    },
    {
        "id": "IchimokuTkCrossStrategy",
        "num": 29,
        "ui_order": 29,
        "name": "Ichimoku TK cross",
        "desc": "ML pack · sim new_ichimoku · test ML PnL 31.8 USDT · SL -2.2% · TP 1.6% · gate>=55% · exp exp10_xgb_pos_weight",
    },
    {
        "id": "SupertrendRsiObvComboStrategy",
        "num": 30,
        "ui_order": 30,
        "name": "Supertrend+RSI+OBV",
        "desc": "ML pack · sim combo_st_rsi_obv · test ML PnL 31.0 USDT · SL -1.9% · TP 1.5% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "AdxMomentumStrategy",
        "num": 32,
        "ui_order": 31,
        "name": "Breakout-Retest",
        "desc": "ML pack · sim trend_breakout · test ML PnL 928.0 USDT · SL -2.2% · TP 3.0% · gate>=70% · legacy_april_cut",
    },
    {
        "id": "BollingerRsiStrategy",
        "num": 33,
        "ui_order": 32,
        "name": "Mean-reversion (BB)",
        "desc": "ML pack · sim lite_mean_rev · test ML PnL 513.8 USDT · SL -2.0% · TP 2.5% · gate>=70% · legacy_april_cut",
    },
    {
        "id": "MacdEmaStrategy",
        "num": 34,
        "ui_order": 33,
        "name": "MACD + EMA200",
        "desc": "ML pack · sim trend_macd_ema · test ML PnL 450.0 USDT · SL -2.5% · TP 3.0% · gate>=45% · legacy_april_cut",
    },
    {
        "id": "SupertrendStrategy",
        "num": 35,
        "ui_order": 34,
        "name": "Supertrend (ATR)",
        "desc": "ML pack · sim trend_supertrend · test ML PnL 157.4 USDT · SL -2.5% · TP 3.0% · gate>=70% · legacy_april_cut",
    },
    {
        "id": "TripleEmaStrategy",
        "num": 36,
        "ui_order": 35,
        "name": "EMA 50/200 (4H)",
        "desc": "ML pack · sim trend_ema · test ML PnL 152.3 USDT · SL -2.5% · TP 4.0% · gate>=55% · legacy_april_cut",
    },
    {
        "id": "LiteRangeStrategy",
        "num": 37,
        "ui_order": 36,
        "name": "Диапазонная",
        "desc": "ML pack · sim lite_range · test ML PnL 88.0 USDT · SL -1.8% · TP 2.2% · gate>=65% · legacy_april_cut",
    },
    {
        "id": "LiteIntradayStrategy",
        "num": 38,
        "ui_order": 37,
        "name": "Внутридневная",
        "desc": "ML pack · sim lite_intraday · test ML PnL 18.9 USDT · SL -2.2% · TP 2.2% · gate>=45% · legacy_april_cut",
    },
    {
        "id": "FibPullbackStrategy",
        "num": 39,
        "ui_order": 38,
        "name": "Fib pullback (DCA)",
        "desc": "ML pack · sim trend_fib · test ML PnL 15.3 USDT · SL -3.0% · TP 3.5% · gate>=45% · legacy_april_cut",
    },
    {
        "id": "CriptoPairsStrategy",
        "num": None,
        "ui_order": 1000,
        "name": "CriptoPairs",
        "desc": "Legacy · off by default. Kept in catalog for history/stats labels.",
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
          AND cd_key IN ('ml_confidence', 'ml_gate_confidence', 'ml_predicted', 'ml_min_confidence')
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


# Hard safety cap for closed-trade dumps (UI history / stats "all time").
CLOSED_TRADES_HARD_CAP = 50000


def load_closed_trades_from_db(bot: str, limit: int = 500) -> list[dict[str, Any]]:
    """Fast closed-trade list from sqlite (no bot RPC / orders payload).

    ``limit <= 0`` means return all closed trades (up to CLOSED_TRADES_HARD_CAP).
    """
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        return []
    db_path = _bot_db_path(bot)
    if not db_path:
        return []
    if int(limit) <= 0:
        limit = CLOSED_TRADES_HARD_CAP
    else:
        limit = max(1, min(int(limit), CLOSED_TRADES_HARD_CAP))
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
        # Prefer RW so WAL pages from the live bot process are visible.
        with sqlite3.connect(db_path) as conn:
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
    # limit <= 0 → all (capped server-side)
    if int(limit) > 0:
        limit = max(1, min(int(limit), CLOSED_TRADES_HARD_CAP))
    if bot:
        bot = resolve_bot(bot)
        if bot not in CONFIGS:
            return {"error": "invalid bot"}
        return {"bot": bot, "trades": load_closed_trades_from_db(bot, limit)}
    return {name: load_closed_trades_from_db(name, limit) for name in CONFIGS}


def _strip_enter_tag(tag: str | None) -> str:
    if not tag:
        return ""
    t = str(tag)
    changed = True
    while changed:
        changed = False
        for suffix in (":hedge", ":inv"):
            if t.lower().endswith(suffix):
                t = t[: -len(suffix)]
                changed = True
    return t


def _parse_close_date_ms(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # seconds or ms
        n = float(value)
        return n * 1000.0 if n < 1e12 else n
    s = str(value).strip().replace("T", " ")
    if not s:
        return None
    for fmt, cut in (("%Y-%m-%d %H:%M:%S.%f", 26), ("%Y-%m-%d %H:%M:%S", 19)):
        try:
            return datetime.strptime(s[:cut], fmt).timestamp() * 1000.0
        except ValueError:
            continue
    return None


def _rating_period_bounds_ms(period: str) -> tuple[float, float]:
    now = datetime.now()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_today = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    if period == "today":
        return start_today.timestamp() * 1000.0, end_today.timestamp() * 1000.0
    if period == "yesterday":
        y0 = start_today.timestamp() * 1000.0 - 86400000.0
        y1 = start_today.timestamp() * 1000.0 - 1.0
        return y0, y1
    if period == "7d":
        return now.timestamp() * 1000.0 - 7 * 86400000.0, float("inf")
    if period == "30d":
        return now.timestamp() * 1000.0 - 30 * 86400000.0, float("inf")
    return 0.0, float("inf")


def _resolve_sqlite_from_config(cfg_path: Path) -> Path | None:
    if not cfg_path.is_file():
        return None
    try:
        db_url = str(load_config(cfg_path).get("db_url") or "")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not db_url.startswith("sqlite:///"):
        return None
    name = db_url.replace("sqlite:///", "")
    for candidate in (BASE / name, BASE / "user_data" / name, cfg_path.parent / Path(name).name):
        db_path = candidate.resolve()
        if db_path.is_file():
            return db_path
    return None


def iter_all_strategy_db_paths() -> list[tuple[str, Path]]:
    """Admin + every tenant strategy sqlite (global rating, ignores request tenant)."""
    return [(uid, path) for uid, bot, path in iter_all_trade_db_paths() if bot == "strategy"]


_BOT_CONFIG_NAMES = {
    "strategy": "config_strategy.json",
    "finder": "config.json",
    "grid": "config_grid.json",
}
_BOT_DB_FALLBACKS = {
    "strategy": ("tradesv3-strategy.sqlite",),
    "finder": ("tradesv3-finder.sqlite", "tradesv3.sqlite"),
    "grid": ("tradesv3-grid.sqlite",),
}
_BOT_RATING_LABELS = {
    "strategy": "Стратегии",
    "finder": "ML Finder",
    "grid": "Grid",
}


def iter_all_trade_db_paths() -> list[tuple[str, str, Path]]:
    """All users × bots sqlite paths: (user_id, bot, path)."""
    found: dict[tuple[str, str], Path] = {}

    def _add(user_id: str, bot: str, path: Path | None) -> None:
        if path is None or not path.is_file():
            return
        key = (user_id, bot)
        if key not in found:
            found[key] = path.resolve()

    for bot, cfg_path in _ADMIN_CONFIGS.items():
        _add("admin", bot, _resolve_sqlite_from_config(cfg_path))
        for name in _BOT_DB_FALLBACKS.get(bot, ()):
            for candidate in (BASE / name, BASE / "user_data" / name):
                if ("admin", bot) not in found and candidate.is_file():
                    _add("admin", bot, candidate)

    tenants_root = BASE / "user_data" / "tenants"
    if tenants_root.is_dir():
        for td in sorted(tenants_root.iterdir()):
            if not td.is_dir():
                continue
            uid = td.name
            for bot, cfg_name in _BOT_CONFIG_NAMES.items():
                _add(uid, bot, _resolve_sqlite_from_config(td / cfg_name))
                if (uid, bot) not in found:
                    for name in _BOT_DB_FALLBACKS.get(bot, ()):
                        cand = td / name
                        if cand.is_file():
                            _add(uid, bot, cand)
                            break
    return [(uid, bot, path) for (uid, bot), path in sorted(found.items())]


def _strategy_label_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for s in AVAILABLE_STRATEGIES:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        num = s.get("num")
        name = str(s.get("name") or sid)
        if num is not None and str(num).strip() != "":
            try:
                out[sid] = f"#{int(num)} {name}"
            except (TypeError, ValueError):
                out[sid] = name
        else:
            out[sid] = name
    return out


def _new_rating_bucket(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "pnl": 0.0,
        "income": 0.0,
        "loss_sum": 0.0,
    }
    if extra:
        row.update(extra)
    return row


def _accumulate_rating(row: dict[str, Any], pnl: float) -> None:
    row["trades"] += 1
    row["pnl"] += pnl
    if pnl > 1e-12:
        row["wins"] += 1
        row["income"] += pnl
    elif pnl < -1e-12:
        row["losses"] += 1
        row["loss_sum"] += pnl
    else:
        row["flat"] += 1


def _finalize_rating_row(row: dict[str, Any]) -> dict[str, Any]:
    row["pnl"] = round(float(row["pnl"]), 4)
    row["income"] = round(float(row["income"]), 4)
    row["loss_sum"] = round(float(row["loss_sum"]), 4)
    row["loss_abs"] = round(abs(float(row["loss_sum"])), 4)
    n = int(row["trades"]) or 1
    row["winrate"] = round(100.0 * int(row["wins"]) / n, 1)
    row["avg_pnl"] = round(float(row["pnl"]) / n, 4)
    return row


def _rank_rating_rows(items: list[dict[str, Any]], *, limit: int, key: str = "pnl") -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda x: float(x.get(key) or 0.0), reverse=True)
    out: list[dict[str, Any]] = []
    for i, item in enumerate(ordered[:limit], 1):
        out.append({"rank": i, **item})
    return out


def _normalize_rating_user_filter(user_id: str | None) -> str | None:
    """Empty / 'all' → None (all accounts). Otherwise return stripped user id."""
    raw = str(user_id or "").strip()
    if not raw or raw.lower() in {"all", "*"}:
        return None
    return raw


def _rating_user_label(user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = tm.get_user_by_id(user_id)
    if not user:
        return user_id
    username = str(user.get("username") or user_id)
    if str(user.get("role", "")).lower() == "admin" or str(user.get("id")) == "admin":
        return f"Главный ({username})"
    return username


def _resolve_rating_user_filter(user_id: str | None) -> tuple[str | None, str | None]:
    """Return (filter_id, label). Raises ValueError if id is unknown."""
    filtered = _normalize_rating_user_filter(user_id)
    if filtered is None:
        return None, None
    if tm.get_user_by_id(filtered) is None:
        raise ValueError(f"unknown user: {filtered}")
    return filtered, _rating_user_label(filtered)


def get_strategy_rating_payload(
    period: str = "all", limit: int = 40, user_id: str | None = None
) -> dict[str, Any]:
    """Top strategies by net PnL across all users' strategy bots (or one user)."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 40), 100))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    agg: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT enter_tag, close_date, close_profit_abs
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, db_path in iter_all_strategy_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(query).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("strategy rating %s %s: %s", uid, db_path, exc)
            sources.append({"user": uid, "db": str(db_path), "trades": 0, "error": str(exc)})
            continue
        for enter_tag, close_date, profit_abs in rows:
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            sid = _strip_enter_tag(enter_tag) or "Без тега"
            try:
                pnl = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            row = agg.setdefault(
                sid,
                _new_rating_bucket({"strategy_id": sid, "label": labels.get(sid, sid)}),
            )
            _accumulate_rating(row, pnl)
            used += 1
        sources.append({"user": uid, "db": str(db_path.name), "trades": used})

    rows = [_finalize_rating_row(r) for r in agg.values()]
    ranked = _rank_rating_rows(rows, limit=limit, key="pnl")
    return {
        "period": period,
        "limit": limit,
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len({s.get("user") for s in sources}),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "strategies_total": len(rows),
        "rows": ranked,
        "pnl": ranked,
        "losses": _rank_rating_rows(rows, limit=limit, key="loss_abs"),
        "income": _rank_rating_rows(rows, limit=limit, key="income"),
    }


def get_pair_rating_payload(
    period: str = "all", limit: int = 100, user_id: str | None = None
) -> dict[str, Any]:
    """Pair ranking across all users/bots (or one user), with strategy+bot detail."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 100), 200))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    # pair -> aggregates + details keyed by (bot, strategy_id)
    agg: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT pair, enter_tag, strategy, close_date, close_profit_abs
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, bot, db_path in iter_all_trade_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(query).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("pair rating %s/%s %s: %s", uid, bot, db_path, exc)
            sources.append(
                {"user": uid, "bot": bot, "db": str(db_path), "trades": 0, "error": str(exc)}
            )
            continue
        bot_label = _BOT_RATING_LABELS.get(bot, bot)
        for pair, enter_tag, strategy_name, close_date, profit_abs in rows:
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            pair_id = str(pair or "").strip() or "—"
            sid = _strip_enter_tag(enter_tag)
            if not sid:
                sid = str(strategy_name or "").strip() or bot_label
            try:
                pnl = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0

            prow = agg.setdefault(
                pair_id,
                _new_rating_bucket(
                    {
                        "pair": pair_id,
                        "label": pair_id,
                        "_details": {},
                    }
                ),
            )
            _accumulate_rating(prow, pnl)

            detail_key = f"{bot}|{sid}"
            dmap: dict[str, Any] = prow["_details"]
            drow = dmap.setdefault(
                detail_key,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_label": labels.get(sid, sid),
                        "bot": bot,
                        "bot_label": bot_label,
                        "label": f"{labels.get(sid, sid)} · {bot_label}",
                    }
                ),
            )
            _accumulate_rating(drow, pnl)
            used += 1
        sources.append({"user": uid, "bot": bot, "db": str(db_path.name), "trades": used})

    rows: list[dict[str, Any]] = []
    for prow in agg.values():
        details_raw = list((prow.pop("_details", {}) or {}).values())
        details = [_finalize_rating_row(d) for d in details_raw]
        details.sort(key=lambda x: float(x.get("pnl") or 0.0), reverse=True)
        row = _finalize_rating_row(prow)
        row["details"] = details
        row["details_count"] = len(details)
        rows.append(row)

    ranked = _rank_rating_rows(rows, limit=limit, key="pnl")
    users = {s.get("user") for s in sources}
    return {
        "period": period,
        "limit": limit,
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len(users),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "pairs_total": len(rows),
        "rows": ranked,
    }


def _default_bot_limits_payload() -> dict[str, Any]:
    return {
        "max_open_trades": dict(DEFAULT_BOT_LIMITS),
        "stake_amount": dict(DEFAULT_STAKES),
        "strategy_risk": {
            "stoploss": DEFAULT_STRATEGY_STOPLOSS,
            "take_profit": DEFAULT_STRATEGY_TAKE_PROFIT,
        },
        "max_open_trades_per_strategy": DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY,
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
    if bot_limits_file().is_file():
        stored = json.loads(bot_limits_file().read_text(encoding="utf-8")).get("strategy_risk", {})
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
    payload = load_bot_limits_file() if bot_limits_file().is_file() else _default_bot_limits_payload()
    payload["strategy_risk"] = {
        "stoploss": float(risk["stoploss"]),
        "take_profit": float(risk["take_profit"]),
    }
    bot_limits_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = bot_limits_file().with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(bot_limits_file())


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
    if not bot_limits_file().is_file():
        return ensure_bot_limits_snapshot()
    data = json.loads(bot_limits_file().read_text(encoding="utf-8"))
    payload = _default_bot_limits_payload()
    stored_trades = data.get("max_open_trades", data if "stake_amount" not in data else {})
    for bot in CONFIGS:
        if bot in stored_trades:
            payload["max_open_trades"][bot] = int(stored_trades[bot])
    stored_stakes = data.get("stake_amount", {})
    for bot, default in DEFAULT_STAKES.items():
        if bot in stored_stakes:
            payload["stake_amount"][bot] = float(stored_stakes[bot])
        else:
            payload["stake_amount"][bot] = float(default)
    if "max_open_trades_per_strategy" in data:
        try:
            payload["max_open_trades_per_strategy"] = _normalize_max_open_trades_per_strategy(
                int(data["max_open_trades_per_strategy"])
            )
        except (TypeError, ValueError):
            pass
    stored_risk = data.get("strategy_risk")
    if isinstance(stored_risk, dict):
        risk = dict(payload.get("strategy_risk") or {})
        if "stoploss" in stored_risk:
            try:
                risk["stoploss"] = float(stored_risk["stoploss"])
            except (TypeError, ValueError):
                pass
        if "take_profit" in stored_risk:
            try:
                risk["take_profit"] = float(stored_risk["take_profit"])
            except (TypeError, ValueError):
                pass
        payload["strategy_risk"] = risk
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
    if bot_limits_file().is_file():
        return load_bot_limits()
    return snapshot_limits_from_configs()


def save_bot_limits(
    limits: dict[str, int],
    *,
    grid_stake: float | None = None,
    stakes: dict[str, float] | None = None,
    max_open_trades_per_strategy: int | None = None,
) -> None:
    bot_limits_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = bot_limits_file().with_suffix(".json.tmp")
    payload = load_bot_limits_file() if bot_limits_file().is_file() else _default_bot_limits_payload()
    payload["max_open_trades"] = {bot: int(limits[bot]) for bot in CONFIGS if bot in limits}
    if stakes:
        for bot, value in stakes.items():
            if bot in DEFAULT_STAKES:
                payload["stake_amount"][bot] = float(value)
    if grid_stake is not None:
        payload["stake_amount"]["grid"] = float(grid_stake)
    if max_open_trades_per_strategy is not None:
        payload["max_open_trades_per_strategy"] = int(max_open_trades_per_strategy)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(bot_limits_file())


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
    # Keep dedicated per-strategy file in sync with bot_limits (source of truth on restart).
    # Prefer bot_limits key; if missing, keep existing dedicated file instead of forcing 0.
    try:
        if "max_open_trades_per_strategy" in payload:
            raw_per = int(payload["max_open_trades_per_strategy"])
        elif max_open_trades_per_strategy_file().is_file():
            raw_per = load_max_open_trades_per_strategy()
        else:
            raw_per = DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY
        _save_max_open_trades_per_strategy_file(int(raw_per))
        if payload.get("max_open_trades_per_strategy") != int(raw_per):
            payload["max_open_trades_per_strategy"] = int(raw_per)
            save_bot_limits(
                {bot: int(payload["max_open_trades"][bot]) for bot in CONFIGS},
                stakes=dict(payload.get("stake_amount") or {}),
                max_open_trades_per_strategy=int(raw_per),
            )
    except (TypeError, ValueError):
        _save_max_open_trades_per_strategy_file(DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY)
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


def stop_bot(bot: str) -> Any:
    return api_call(STOP[bot], "POST", retries=3, retry_delay=1.0)


def start_bot(bot: str) -> Any:
    return api_call(START[bot], "POST", retries=3, retry_delay=1.0)


def bot_trading_disabled(bot: str) -> bool:
    """True when max_open_trades is 0 — bot must not open new trades."""
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        return False
    try:
        cfg = load_config(CONFIGS[bot])
        return int(cfg.get("max_open_trades") or 0) <= 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


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
    user = tc.current_user()
    if user and not tm.is_admin(user):
        api_user, api_pass, _jwt = tm.derive_api_creds(str(user["id"]))
    else:
        api_user, api_pass = AUTH_USER, AUTH_PASS
    token = base64.b64encode(f"{api_user}:{api_pass}".encode()).decode()
    return f"Basic {token}"


def _strategy_ids() -> set[str]:
    return {s["id"] for s in AVAILABLE_STRATEGIES}


def get_strategy_catalog() -> list[dict]:
    """Catalog for UI. Sorted by num ascending; strategies without num go last."""
    rows = list(AVAILABLE_STRATEGIES)

    def _sort_key(s: dict) -> tuple:
        has_num = s.get("num") is not None
        num = int(s["num"]) if has_num else 10_000
        return (0 if has_num else 1, num, s["id"])

    rows.sort(key=_sort_key)
    return rows


def validate_strategy(strategy_id: str) -> str:
    strategy_id = strategy_id.strip()
    if strategy_id not in _strategy_ids():
        raise ValueError("unknown strategy")
    path = STRATEGIES_DIR / f"{strategy_id}.py"
    if not path.is_file():
        raise ValueError(f"strategy file missing: {strategy_id}")
    return strategy_id


PROD_DEFAULT_STRATEGIES = frozenset({
    "AltVolumeBreakoutStrategy",
    "PsaraFlipStrategy",
    "AtrChannelBreakoutStrategy",
    "CmfZeroCrossStrategy",
    "ScalpEmaCrossStrategy",
    "ChaikinOscStrategy",
    "DonchianBreakoutStrategy",
    "PpoSignalStrategy",
    "DonchianAdxVolComboStrategy",
    "ObvEmaCrossStrategy",
    "ElderRayStrategy",
    "ScalpMacdHistStrategy",
    "KeltnerBreakoutStrategy",
    "HeikinAshiFlipStrategy",
    "VortexCrossStrategy",
    "AwesomeOscStrategy",
    "KeltnerStochVolComboStrategy",
    "TemaCrossStrategy",
    "TrixSignalStrategy",
    "RocMomentumStrategy",
    "HmaPpoAtrComboStrategy",
    "WilliamsRReclaimStrategy",
    "BbSqueezeBreakoutStrategy",
    "EmaRsiAtrComboStrategy",
    "AroonCrossStrategy",
    "AdxMacdVolComboStrategy",
    "EngulfingTrendStrategy",
    "AdxDiCrossStrategy",
    "MfiReclaimStrategy",
    "IchimokuTkCrossStrategy",
    "SupertrendRsiObvComboStrategy",
})


def default_enabled_map() -> dict[str, bool]:
    return {s["id"]: s["id"] in PROD_DEFAULT_STRATEGIES for s in AVAILABLE_STRATEGIES}


def load_enabled_map() -> dict[str, bool]:
    if not enabled_strategies_file().is_file():
        return default_enabled_map()
    data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    enabled = data.get("enabled", {})
    result = default_enabled_map()
    for sid in result:
        if sid in enabled:
            result[sid] = bool(enabled[sid])
    return result


def load_inverted_map() -> dict[str, bool]:
    result = {s["id"]: False for s in AVAILABLE_STRATEGIES}
    if not enabled_strategies_file().is_file():
        return result
    try:
        data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    inverted = data.get("inverted", {})
    for sid in result:
        if sid in inverted:
            result[sid] = bool(inverted[sid])
    return result


def _trained_risk_catalog() -> dict[str, dict[str, Any]]:
    """SL/ROI from prod_top30_pack (sim training) keyed by strategy class."""
    pack_path = BASE.parent / "simulation" / "config" / "prod_top30_pack.json"
    # monorepo: site/../simulation ; VPS: app/simulation
    candidates = [
        BASE.parent / "simulation" / "config" / "prod_top30_pack.json",
        BASE / "simulation" / "config" / "prod_top30_pack.json",
        Path(__file__).resolve().parents[2] / "simulation" / "config" / "prod_top30_pack.json",
    ]
    for pack_path in candidates:
        if pack_path.is_file():
            try:
                pack = json.loads(pack_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out: dict[str, dict[str, Any]] = {}
            for s in pack.get("strategies") or []:
                cls = s.get("class_name")
                if not cls:
                    continue
                out[cls] = {
                    "stoploss": float(s.get("stoploss") or 0),
                    "tp": float(s.get("tp") or 0),
                    "minimal_roi": s.get("minimal_roi") or {"0": float(s.get("tp") or 0)},
                    "scenario_id": s.get("scenario_id"),
                    "min_profit_proba": float(s.get("min_profit_proba") or 0.55),
                }
            return out
    return {}


ML_CONFIDENCE_CHOICES: tuple[float, ...] = (
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
)


def _normalize_ml_confidence(value: float) -> float:
    """Snap to nearest allowed confidence choice."""
    v = float(value)
    best = ML_CONFIDENCE_CHOICES[0]
    best_d = abs(best - v)
    for choice in ML_CONFIDENCE_CHOICES[1:]:
        d = abs(choice - v)
        if d < best_d:
            best = choice
            best_d = d
    return float(best)


def _ml_confidence_defaults() -> dict[str, float]:
    """Default ML gate confidence per strategy class (from pack / model meta)."""
    catalog = _trained_risk_catalog()
    result: dict[str, float] = {}
    for s in AVAILABLE_STRATEGIES:
        sid = s["id"]
        if sid in catalog and catalog[sid].get("min_profit_proba") is not None:
            result[sid] = _normalize_ml_confidence(float(catalog[sid]["min_profit_proba"]))
        else:
            result[sid] = 0.55
    return result


def load_ml_confidence_map() -> dict[str, float]:
    defaults = _ml_confidence_defaults()
    result = dict(defaults)
    if not enabled_strategies_file().is_file():
        return result
    try:
        data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    stored = data.get("ml_confidence") or {}
    for sid in result:
        if sid in stored:
            try:
                result[sid] = _normalize_ml_confidence(float(stored[sid]))
            except (TypeError, ValueError):
                pass
    return result


def load_trained_risk_map() -> dict[str, bool]:
    catalog = _trained_risk_catalog()
    result = {s["id"]: (s["id"] in catalog) for s in AVAILABLE_STRATEGIES}
    if not enabled_strategies_file().is_file():
        return result
    try:
        data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    stored = data.get("trained_risk") or {}
    for sid in result:
        if sid in stored:
            result[sid] = bool(stored[sid])
        if sid not in catalog:
            result[sid] = False
    return result


def _read_enabled_file() -> dict[str, Any]:
    if not enabled_strategies_file().is_file():
        return {}
    try:
        return json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_enabled_map(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
    trained_risk: dict[str, bool] | None = None,
    ml_confidence: dict[str, float] | None = None,
) -> None:
    enabled_strategies_file().parent.mkdir(parents=True, exist_ok=True)
    data = _read_enabled_file()
    data["enabled"] = enabled
    if inverted is not None:
        data["inverted"] = {sid: bool(inverted.get(sid, False)) for sid in enabled}
    else:
        prev = data.get("inverted", {})
        data["inverted"] = {sid: bool(prev.get(sid, False)) for sid in enabled}
    if trained_risk is not None:
        data["trained_risk"] = {sid: bool(trained_risk.get(sid, False)) for sid in enabled}
    else:
        prev_tr = data.get("trained_risk", {})
        catalog = _trained_risk_catalog()
        data["trained_risk"] = {
            sid: bool(prev_tr.get(sid, sid in catalog)) for sid in enabled
        }
        for sid in data["trained_risk"]:
            if sid not in catalog:
                data["trained_risk"][sid] = False
    defaults_ml = _ml_confidence_defaults()
    if ml_confidence is not None:
        data["ml_confidence"] = {
            sid: _normalize_ml_confidence(float(ml_confidence.get(sid, defaults_ml.get(sid, 0.55))))
            for sid in enabled
        }
    else:
        prev_ml = data.get("ml_confidence") or {}
        data["ml_confidence"] = {}
        for sid in enabled:
            if sid in prev_ml:
                try:
                    data["ml_confidence"][sid] = _normalize_ml_confidence(float(prev_ml[sid]))
                except (TypeError, ValueError):
                    data["ml_confidence"][sid] = float(defaults_ml.get(sid, 0.55))
            else:
                data["ml_confidence"][sid] = float(defaults_ml.get(sid, 0.55))
    tmp = enabled_strategies_file().with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(enabled_strategies_file())
    save_strategies_prefs(
        enabled,
        inverted=data["inverted"],
        trained_risk=data["trained_risk"],
        ml_confidence=data["ml_confidence"],
    )


def save_strategies_prefs(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
    trained_risk: dict[str, bool] | None = None,
    ml_confidence: dict[str, float] | None = None,
) -> None:
    bot_strategies_file().parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"enabled": enabled}
    if inverted is not None:
        payload["inverted"] = inverted
    elif bot_strategies_file().is_file():
        try:
            old = json.loads(bot_strategies_file().read_text(encoding="utf-8"))
            if "inverted" in old:
                payload["inverted"] = old["inverted"]
        except (json.JSONDecodeError, OSError):
            pass
    if trained_risk is not None:
        payload["trained_risk"] = trained_risk
    elif bot_strategies_file().is_file():
        try:
            old = json.loads(bot_strategies_file().read_text(encoding="utf-8"))
            if "trained_risk" in old:
                payload["trained_risk"] = old["trained_risk"]
        except (json.JSONDecodeError, OSError):
            pass
    if ml_confidence is not None:
        payload["ml_confidence"] = ml_confidence
    elif bot_strategies_file().is_file():
        try:
            old = json.loads(bot_strategies_file().read_text(encoding="utf-8"))
            if "ml_confidence" in old:
                payload["ml_confidence"] = old["ml_confidence"]
        except (json.JSONDecodeError, OSError):
            pass
    tmp = bot_strategies_file().with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(bot_strategies_file())


def snapshot_strategies_from_file() -> dict[str, bool]:
    """Persist current enabled strategies before deploy overwrites anything."""
    enabled = load_enabled_map()
    save_strategies_prefs(enabled)
    return enabled


def apply_strategies_prefs() -> dict[str, bool]:
    """Restore enabled strategies from bot_strategies.json after deploy."""
    if not bot_strategies_file().is_file():
        return load_enabled_map()
    data = json.loads(bot_strategies_file().read_text(encoding="utf-8"))
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
    trained = load_trained_risk_map()
    stored_tr = data.get("trained_risk", {})
    for sid in trained:
        if sid in stored_tr:
            trained[sid] = bool(stored_tr[sid])
    ml_conf = load_ml_confidence_map()
    stored_ml = data.get("ml_confidence", {})
    for sid in ml_conf:
        if sid in stored_ml:
            try:
                ml_conf[sid] = _normalize_ml_confidence(float(stored_ml[sid]))
            except (TypeError, ValueError):
                pass
    save_enabled_map(result, inverted=inverted, trained_risk=trained, ml_confidence=ml_conf)
    return result


def ensure_router_config() -> None:
    cfg = load_config(CONFIGS["strategy"])
    if cfg.get("strategy") != ROUTER_STRATEGY:
        cfg["strategy"] = ROUTER_STRATEGY
        save_config(CONFIGS["strategy"], cfg)


def load_dual_hedge_enabled() -> bool:
    if not dual_hedge_file().is_file():
        return False
    try:
        data = json.loads(dual_hedge_file().read_text(encoding="utf-8"))
        return bool(data.get("enabled", False))
    except (json.JSONDecodeError, OSError):
        return False


def _normalize_max_open_trades_per_strategy(value: int) -> int:
    value = int(value)
    if value < 0 or value > MAX_MAX_TRADES:
        raise ValueError(
            f"max_open_trades_per_strategy must be between 0 and {MAX_MAX_TRADES} (0 = unlimited)"
        )
    return value


def load_max_open_trades_per_strategy() -> int:
    """0 = unlimited (only global max_open_trades). Router reads the json file live."""
    path = max_open_trades_per_strategy_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _normalize_max_open_trades_per_strategy(int(data.get("value", 0)))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    if bot_limits_file().is_file():
        try:
            stored = json.loads(bot_limits_file().read_text(encoding="utf-8")).get(
                "max_open_trades_per_strategy"
            )
            if stored is not None:
                return _normalize_max_open_trades_per_strategy(int(stored))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    return DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY


def _save_max_open_trades_per_strategy_file(value: int) -> None:
    value = _normalize_max_open_trades_per_strategy(value)
    path = max_open_trades_per_strategy_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"value": value}
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def set_max_open_trades_per_strategy(value: int) -> dict[str, Any]:
    value = _normalize_max_open_trades_per_strategy(value)
    old = load_max_open_trades_per_strategy()
    _save_max_open_trades_per_strategy_file(value)
    limits = load_bot_limits()
    save_bot_limits(limits, max_open_trades_per_strategy=value)
    try:
        record_max_open_trades_per_strategy(old, value)
    except Exception:
        pass
    # Router reads the file on each entry — no bot reload required.
    return {
        "max_open_trades_per_strategy": value,
        "reload_required": False,
        **get_strategies_payload(),
    }


def _save_dual_hedge_file(enabled: bool) -> None:
    dual_hedge_file().parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "note": (
            "При включении на каждый сигнал открываются две позиции: по направлению сигнала "
            "и противоположная (хедж). Требует hedge mode на Bybit."
        ),
    }
    tmp = dual_hedge_file().with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(dual_hedge_file())


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
    trained_risk = load_trained_risk_map()
    catalog = _trained_risk_catalog()
    ml_confidence = load_ml_confidence_map()
    ml_defaults = _ml_confidence_defaults()
    return {
        "router": ROUTER_STRATEGY,
        "enabled": enabled,
        "enabled_ids": enabled_ids,
        "enabled_count": len(enabled_ids),
        "strategies": get_strategy_catalog(),
        "risk": strategy_risk_payload(),
        "dual_hedge": dual_hedge,
        "max_open_trades_per_strategy": load_max_open_trades_per_strategy(),
        "inverted": inverted,
        "trained_risk": trained_risk,
        "trained_risk_available": {sid: (sid in catalog) for sid in enabled},
        "trained_risk_specs": catalog,
        "ml_confidence": ml_confidence,
        "ml_confidence_defaults": ml_defaults,
        "ml_confidence_choices": list(ML_CONFIDENCE_CHOICES),
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


def set_all_strategies_enabled(enabled: bool) -> dict[str, Any]:
    """Enable or disable strategies in bulk.

    Disable-all: every catalog strategy off (pause new entries).
    Enable-all: turn on current prod pack strategies; leave legacy catalog ids off.
    """
    prev = load_enabled_map()
    pack_ids: set[str] = set()
    legacy_ids: set[str] = set()
    candidates = [
        BASE.parent / "simulation" / "config" / "prod_top30_pack.json",
        BASE / "simulation" / "config" / "prod_top30_pack.json",
        Path(__file__).resolve().parents[2] / "simulation" / "config" / "prod_top30_pack.json",
    ]
    for pack_path in candidates:
        if not pack_path.is_file():
            continue
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        pack_ids = {
            str(s.get("class_name") or "")
            for s in (pack.get("strategies") or [])
            if s.get("class_name")
        }
        legacy_ids = {str(x) for x in (pack.get("legacy_disabled") or [])}
        break
    next_state: dict[str, bool] = {}
    for s in AVAILABLE_STRATEGIES:
        sid = s["id"]
        if not enabled:
            next_state[sid] = False
        elif sid in legacy_ids:
            next_state[sid] = False
        elif pack_ids:
            next_state[sid] = sid in pack_ids
        else:
            next_state[sid] = True
    save_enabled_map(next_state)
    ensure_router_config()
    try:
        for sid, on in next_state.items():
            if prev.get(sid) != on:
                record_strategy_toggle(sid, on)
    except Exception:
        pass
    return {
        "enabled_all": bool(enabled),
        "enabled_count": sum(1 for on in next_state.values() if on),
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


def toggle_strategy_trained_risk(strategy_id: str, trained_risk: bool) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    catalog = _trained_risk_catalog()
    if trained_risk and strategy_id not in catalog:
        raise ValueError("no trained SL/TP for this strategy")
    enabled = load_enabled_map()
    state = load_trained_risk_map()
    state[strategy_id] = bool(trained_risk)
    save_enabled_map(enabled, trained_risk=state)
    return {
        "strategy": strategy_id,
        "trained_risk": state[strategy_id],
        **get_strategies_payload(),
    }


def set_strategy_ml_confidence(strategy_id: str, confidence: float) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    raw = float(confidence)
    if raw > 1.0:
        raw = raw / 100.0
    conf = _normalize_ml_confidence(raw)
    enabled = load_enabled_map()
    state = load_ml_confidence_map()
    state[strategy_id] = conf
    save_enabled_map(enabled, ml_confidence=state)
    return {
        "strategy": strategy_id,
        "ml_confidence": state[strategy_id],
        **get_strategies_payload(),
    }


def set_all_strategies_ml_confidence(
    confidence: float | None = None,
    *,
    reset: bool = False,
) -> dict[str, Any]:
    """Set the same ML confidence for every catalog strategy, or restore pack defaults."""
    enabled = load_enabled_map()
    defaults = _ml_confidence_defaults()
    if reset:
        state = {sid: float(val) for sid, val in defaults.items()}
        bulk_value: float | str = "default"
    else:
        if confidence is None:
            raise ValueError("ml_confidence required")
        raw = float(confidence)
        if raw > 1.0:
            raw = raw / 100.0
        conf = _normalize_ml_confidence(raw)
        state = {s["id"]: conf for s in AVAILABLE_STRATEGIES}
        bulk_value = conf
    save_enabled_map(enabled, ml_confidence=state)
    return {
        "ml_confidence_bulk": bulk_value,
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
        state["max_open_trades_per_strategy"] = load_max_open_trades_per_strategy()
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

    trade_action: str | None = None
    trade_result: Any = None
    trade_warning: str | None = None
    # 0 slots → stop trading; restore ≥1 after 0 → start again
    if value <= 0:
        trade_action = "stop"
        try:
            trade_result = stop_bot(bot)
        except BaseException as exc:  # noqa: BLE001
            trade_warning = str(exc)
    elif old_val <= 0 and value >= 1:
        if bot == "finder" and not is_finder_bot_enabled():
            trade_action = "skip_start_finder_disabled"
        else:
            trade_action = "start"
            try:
                trade_result = start_bot(bot)
            except BaseException as exc:  # noqa: BLE001
                trade_warning = str(exc)

    result: dict[str, Any] = {
        "bot": bot,
        "max_open_trades": value,
        "reloaded": reload_result,
        "state": safe_get_state(bot),
        "trading_disabled": value <= 0,
    }
    if trade_action:
        result["trade_action"] = trade_action
        result["trade_result"] = trade_result
    if reload_warning:
        result["reload_warning"] = reload_warning
    if trade_warning:
        result["trade_warning"] = trade_warning
        if not reload_warning:
            result["reload_warning"] = trade_warning
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
    "finder": ("ML Finder", BASE / "user_data" / "logs" / "cryptotools-finder.log"),
    "strategy": ("Стратегии", BASE / "user_data" / "logs" / "cryptotools-strategy.log"),
    "grid": ("Grid", BASE / "user_data" / "logs" / "cryptotools-grid.log"),
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
    env["CT_BASE"] = str(BASE)
    env.setdefault("CT_ENV", "/home/cryptotools/.cryptotools.env")

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
    env["CT_BASE"] = str(BASE)
    env.setdefault("CT_ENV", "/home/cryptotools/.cryptotools.env")

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
    user: dict[str, Any] | None = None

    def log_message(self, fmt, *args):  # noqa: D401
        _server_log.info("%s - %s", self.address_string(), fmt % args)

    def _authenticate(self) -> dict[str, Any] | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            payload = tm.verify_token(header[7:].strip())
            if not payload:
                return None
            user = tm.get_user_by_id(str(payload.get("sub", "")))
            if not user or not user.get("enabled", True):
                return None
            return user
        if header.startswith("Basic "):
            try:
                raw_user, pwd = base64.b64decode(header[6:]).decode().split(":", 1)
            except Exception:
                return None
            user = tm.get_user_by_username(raw_user)
            if user and tm.verify_password(pwd, str(user.get("password_hash") or "")):
                if not user.get("enabled", True):
                    return None
                return user
            # Legacy Basic auth against FREQUI_* env → admin user
            if (
                AUTH_PASS
                and secrets.compare_digest(raw_user, AUTH_USER)
                and secrets.compare_digest(pwd, AUTH_PASS)
            ):
                admin = tm.get_user_by_id("admin") or tm.get_user_by_username(AUTH_USER)
                if admin and admin.get("enabled", True):
                    return admin
            return None
        return None

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

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return b""
        return self.rfile.read(length)

    def _unauthorized(self) -> None:
        # Do NOT send WWW-Authenticate: Basic — browsers show a native login
        # popup over the UI. Session/Bearer auth is handled by the SPA form.
        self._json(401, {"error": "unauthorized"})

    def _require_admin(self) -> bool:
        if not self.user or not tm.is_admin(self.user):
            self._json(403, {"error": "admin required"})
            return False
        return True

    def _auth_me_payload(self) -> dict[str, Any]:
        user = self.user or {}
        public = {k: v for k, v in user.items() if k != "password_hash"}
        if tm.is_admin(user):
            secrets_st = tm.admin_secrets_status()
            bots_running: dict[str, Any] = {}
            for bot, port in tm.ADMIN_BOT_PORTS.items():
                unit = {
                    "finder": "cryptotools-finder",
                    "strategy": "cryptotools-strategy",
                    "grid": "cryptotools-grid",
                }.get(bot, "")
                state = "unknown"
                if unit:
                    try:
                        cp = subprocess.run(
                            ["systemctl", "is-active", unit],
                            capture_output=True,
                            text=True,
                            timeout=5,
                            check=False,
                        )
                        state = (cp.stdout or "").strip() or "unknown"
                    except (OSError, subprocess.SubprocessError):
                        state = "unknown"
                bots_running[bot] = {
                    "unit": unit,
                    "active": state == "active",
                    "state": state,
                    "port": port,
                }
        else:
            secrets_st = tm.secrets_status(str(user["id"]))
            bots_running = tm.tenant_bots_status(str(user["id"]))
        return {
            "user": public,
            "role": public.get("role"),
            "secrets": secrets_st,
            "bots_running": bots_running,
        }

    def _handle_bot_proxy(self, path: str, parsed) -> None:
        parts = [p for p in path.split("/") if p]
        # parts: bot-proxy, bot, ...
        if len(parts) < 2:
            self._json(400, {"error": "bot required"})
            return
        bot = parts[1]
        rest = "/".join(parts[2:])
        if rest.rstrip("/") == "start" and bot_trading_disabled(bot):
            self._json(
                400,
                {
                    "error": "max_open_trades is 0 — set at least 1 active slot to start trading",
                    "trading_disabled": True,
                },
            )
            return
        qs = parse_qs(parsed.query)
        query = {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}
        body = self._read_raw_body() if self.command.upper() not in ("GET", "HEAD") else b""
        status, headers, body_out = tm.proxy_bot_request(
            self.user or {},
            bot,
            self.command,
            rest,
            query=query or None,
            body_bytes=body or None,
            content_type=self.headers.get("Content-Type"),
        )
        self.send_response(status)
        ct = headers.get("content-type") or "application/octet-stream"
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        method = self.command.upper()

        # Unauthenticated endpoints
        if method == "GET" and path == "/health":
            self._json(200, {"ok": True})
            return
        if method == "POST" and path == "/auth/login":
            data = self._read_json()
            username = str(data.get("username") or "")
            password = str(data.get("password") or "")
            user = tm.get_user_by_username(username)
            if not user or not tm.verify_password(password, str(user.get("password_hash") or "")):
                # Legacy env fallback for admin bootstrap
                if (
                    AUTH_PASS
                    and secrets.compare_digest(username, AUTH_USER)
                    and secrets.compare_digest(password, AUTH_PASS)
                ):
                    user = tm.get_user_by_id("admin") or tm.get_user_by_username(AUTH_USER)
                else:
                    self._json(401, {"error": "invalid credentials"})
                    return
            if not user or not user.get("enabled", True):
                self._json(401, {"error": "invalid credentials"})
                return
            token = tm.issue_token(user)
            public = {k: v for k, v in user.items() if k != "password_hash"}
            self._json(200, {"token": token, "user": public, "role": public.get("role")})
            return

        user = self._authenticate()
        if not user:
            self._unauthorized()
            return

        self.user = user
        ctx_token = tc.set_request_user(user)
        try:
            if path.startswith("/bot-proxy/") or path == "/bot-proxy":
                self._handle_bot_proxy(path, parsed)
                return
            if method == "GET":
                self._handle_get(path, parsed)
            elif method == "POST":
                self._handle_post(path, parsed)
            elif method == "PUT":
                self._handle_put(path, parsed)
            elif method == "PATCH":
                self._handle_patch(path, parsed)
            else:
                self._json(405, {"error": "method not allowed"})
        finally:
            tc.reset_request_user(ctx_token)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _handle_get(self, path: str, parsed) -> None:
        if path == "/auth/me":
            self._json(200, self._auth_me_payload())
            return
        if path == "/users":
            if not self._require_admin():
                return
            self._json(200, {"users": tm.list_users_public()})
            return
        if path == "/secrets/status":
            qs = parse_qs(parsed.query)
            if tm.is_admin(self.user):
                uid = (qs.get("user_id") or [None])[0]
                if uid:
                    self._json(200, tm.secrets_status(str(uid)))
                else:
                    self._json(200, tm.admin_secrets_status())
            else:
                self._json(200, tm.secrets_status(str(self.user["id"])))
            return
        if path == "/system":
            self._json(200, get_system_stats())
            return
        if path == "/pairs":
            payload = {b: safe_get_state(b) for b in CONFIGS}
            mode_path = tc.user_data_dir(BASE) / "pairlist_mode.json"
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
            qs = parse_qs(parsed.query)
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
            qs = parse_qs(parsed.query)
            bot = resolve_bot((qs.get("bot") or [""])[0])
            raw_ids = (qs.get("ids") or [""])[0]
            if bot not in CONFIGS:
                self._json(400, {"error": "invalid bot"})
                return
            trade_ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
            self._json(200, get_trade_ml_meta_payload(bot, trade_ids))
            return
        if path == "/closed-trades":
            qs = parse_qs(parsed.query)
            raw_bot = (qs.get("bot") or [""])[0] or None
            bot = resolve_bot(raw_bot) if raw_bot else None
            try:
                raw_limit = (qs.get("limit") or ["0"])[0]
                limit = 0 if str(raw_limit).lower() in ("0", "all", "") else int(raw_limit)
            except ValueError:
                limit = 0
            payload = get_closed_trades_payload(limit, bot)
            if payload.get("error"):
                self._json(400, payload)
                return
            self._json(200, payload)
            return
        if path == "/strategy-rating":
            qs = parse_qs(parsed.query)
            period = (qs.get("period") or ["all"])[0]
            user_q = (qs.get("user") or qs.get("user_id") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["40"])[0])
            except ValueError:
                limit = 40
            if _normalize_rating_user_filter(user_q) and not tm.is_admin(self.user):
                self._json(403, {"error": "admin required"})
                return
            try:
                self._json(200, get_strategy_rating_payload(period, limit, user_q))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/pair-rating":
            qs = parse_qs(parsed.query)
            period = (qs.get("period") or ["all"])[0]
            user_q = (qs.get("user") or qs.get("user_id") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            if _normalize_rating_user_filter(user_q) and not tm.is_admin(self.user):
                self._json(403, {"error": "admin required"})
                return
            try:
                self._json(200, get_pair_rating_payload(period, limit, user_q))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
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
        if path.startswith("/bybit-grid"):
            ud, env = tc.bybit_context_paths(BASE)
            with tenant_bybit_context(ud, env):
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
                    qs = parse_qs(parsed.query)
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
            self._json(404, {"error": "not found"})
            return
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "pairs" and parts[2] in CONFIGS:
            self._json(200, safe_get_state(parts[2]))
            return
        self._json(404, {"error": "not found"})

    def _handle_post(self, path: str, parsed) -> None:
        data = self._read_json()
        if path == "/users":
            if not self._require_admin():
                return
            try:
                created = tm.create_user(str(data.get("username") or ""), str(data.get("password") or ""))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            bots = None
            st = tm.secrets_status(str(created["id"]))
            if st.get("has_secrets"):
                bots = tm.start_tenant_bots(str(created["id"]))
            self._json(201, {"user": created, "bots_started": bots})
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "users" and parts[3] == "password":
            if not self._require_admin():
                return
            try:
                updated = tm.set_user_password(parts[2], str(data.get("password") or ""))
            except (ValueError, KeyError) as exc:
                code = 404 if isinstance(exc, KeyError) else 400
                self._json(code, {"error": str(exc)})
                return
            self._json(200, {"user": updated})
            return
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
        if path.startswith("/bybit-grid"):
            ud, env = tc.bybit_context_paths(BASE)
            with tenant_bybit_context(ud, env):
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
            self._json(404, {"error": "not found"})
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
            elif action == "set_all_strategies_enabled":
                if "enabled" not in data:
                    self._json(400, {"error": "enabled required"})
                    return
                self._json(200, set_all_strategies_enabled(bool(data["enabled"])))
            elif action == "toggle_strategy_invert":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "inverted" not in data:
                    self._json(400, {"error": "inverted required"})
                    return
                self._json(200, toggle_strategy_invert(strategy_id, bool(data["inverted"])))
            elif action == "toggle_strategy_trained_risk":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "trained_risk" not in data:
                    self._json(400, {"error": "trained_risk required"})
                    return
                self._json(
                    200,
                    toggle_strategy_trained_risk(strategy_id, bool(data["trained_risk"])),
                )
            elif action == "set_strategy_ml_confidence":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "ml_confidence" not in data:
                    self._json(400, {"error": "ml_confidence required"})
                    return
                try:
                    self._json(
                        200,
                        set_strategy_ml_confidence(strategy_id, float(data["ml_confidence"])),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "set_all_strategies_ml_confidence":
                reset = bool(data.get("reset"))
                try:
                    if reset:
                        self._json(200, set_all_strategies_ml_confidence(reset=True))
                    else:
                        if "ml_confidence" not in data:
                            self._json(400, {"error": "ml_confidence required (or reset=true)"})
                            return
                        self._json(
                            200,
                            set_all_strategies_ml_confidence(float(data["ml_confidence"])),
                        )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "set_dual_hedge":
                if "enabled" not in data:
                    self._json(400, {"error": "enabled required"})
                    return
                self._json(200, set_dual_hedge(bool(data["enabled"])))
            elif action == "set_max_open_trades_per_strategy":
                if "max_open_trades_per_strategy" not in data and "value" not in data:
                    self._json(400, {"error": "max_open_trades_per_strategy required"})
                    return
                raw = data.get("max_open_trades_per_strategy", data.get("value"))
                try:
                    self._json(200, set_max_open_trades_per_strategy(int(raw)))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
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
                            "set_strategy, toggle_strategy, set_all_strategies_enabled, "
                            "toggle_strategy_invert, "
                            "toggle_strategy_trained_risk, set_strategy_ml_confidence, "
                            "set_all_strategies_ml_confidence, "
                            "set_dual_hedge, set_max_open_trades_per_strategy, "
                            "or set_strategy_risk"
                        )
                    },
                )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            self._json(502, {"error": exc.read().decode()[:500]})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})

    def _handle_put(self, path: str, parsed) -> None:
        if path != "/secrets":
            self._json(404, {"error": "not found"})
            return
        data = self._read_json()
        qs = parse_qs(parsed.query)
        key = str(data.get("bybit_api_key") or "")
        secret = str(data.get("bybit_api_secret") or "")
        if tm.is_admin(self.user):
            target_id = str(data.get("user_id") or (qs.get("user_id") or [None])[0] or "")
            if not target_id:
                self._json(400, {"error": "user_id required for admin secrets write"})
                return
            if target_id == "admin":
                self._json(400, {"error": "admin secrets come from process env"})
                return
        else:
            if data.get("user_id") and str(data.get("user_id")) != str(self.user["id"]):
                self._json(403, {"error": "cannot write secrets for another user"})
                return
            target_id = str(self.user["id"])
        try:
            tm.save_user_secrets(target_id, key, secret)
            bots = tm.restart_tenant_bots(target_id)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True, "user_id": target_id, "bots_restarted": bots})

    def _handle_patch(self, path: str, parsed) -> None:
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "users":
            self._json(404, {"error": "not found"})
            return
        if not self._require_admin():
            return
        data = self._read_json()
        if "enabled" not in data:
            self._json(400, {"error": "enabled required"})
            return
        try:
            updated = tm.set_user_enabled(parts[2], bool(data["enabled"]))
        except (ValueError, KeyError) as exc:
            code = 404 if isinstance(exc, KeyError) else 400
            self._json(code, {"error": str(exc)})
            return
        self._json(200, {"user": updated})


def main() -> None:
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    global AUTH_USER, AUTH_PASS
    AUTH_USER = os.environ.get("FREQUI_USERNAME", AUTH_USER)
    AUTH_PASS = os.environ.get("FREQUI_PASSWORD", AUTH_PASS)
    tm.ensure_users_migrated()
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
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
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
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    limits = snapshot_limits_from_configs()
    print(json.dumps(limits, ensure_ascii=False))


def snapshot_strategies_cli() -> None:
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    enabled = snapshot_strategies_from_file()
    print(json.dumps(enabled, ensure_ascii=False))


def apply_strategies_cli() -> None:
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
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
