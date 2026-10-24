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
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import tenant_manager as tm
import tenant_context as tc
import user_trading
import user_prefs
import control_plane as control_plane
import bot_reconcile
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
from adaptive_scan_scheduler import (
    get_adaptive_scan_status,
    set_adaptive_scan_settings,
    start_adaptive_scan_scheduler,
)
from reconcile_positions import (
    archive_stale_trade,
    fix_reconcile,
    reconcile as reconcile_positions,
)

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
FINDER_BOT_CFG = BASE / "user_data" / "finder_bot.json"
APP_TZ = timezone(timedelta(hours=3))  # UTC+3 (Moscow, no DST)

# Demo tenants mirror the admin (house) demo stack: same trades, read-only.
DEMO_MIRROR_VIEW_BLOCKS: tuple[str, ...] = (
    "strategy",
    "llm",
    "history",
    "dashboard",
    "rating",
)


def is_demo_mirror_user(user: dict[str, Any] | None = None) -> bool:
    """True for Demo tenants that should see admin trades (not real admin)."""
    u = user if user is not None else tc.current_user()
    return bool(u and tm.is_demo_account(u) and not tm.is_admin(u))


def _admin_house_user() -> dict[str, Any]:
    return tm.get_user_by_id("admin") or {
        "id": "admin",
        "role": "admin",
        "username": "admin",
    }


@contextmanager
def maybe_admin_trade_context() -> Iterator[bool]:
    """Demo viewers temporarily resolve CONFIGS/ports as admin (house demo)."""
    if not is_demo_mirror_user():
        yield False
        return
    token = tc.set_request_user(_admin_house_user())
    try:
        yield True
    finally:
        tc.reset_request_user(token)

_LOG_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?(?: UTC(?:\+3)?)?)"
)
_server_log = logging.getLogger("pair_config")


def _parse_log_ts(line: str) -> str | None:
    """Extract log timestamp and normalize to UTC+3 display string."""
    m = _LOG_TS_RE.match(line.strip())
    if not m:
        return None
    raw = m.group(1)
    if "UTC+3" in raw:
        return raw
    body = raw.replace(" UTC", "").strip().replace("T", " ")
    for fmt, cut in (
        ("%Y-%m-%d %H:%M:%S,%f", 26),
        ("%Y-%m-%d %H:%M:%S.%f", 26),
        ("%Y-%m-%d %H:%M:%S", 19),
    ):
        try:
            dt = datetime.strptime(body[:cut], fmt).replace(tzinfo=timezone.utc)
            local = dt.astimezone(APP_TZ)
            if "," in body or "." in body[19:]:
                return (
                    local.strftime("%Y-%m-%d %H:%M:%S,")
                    + f"{int(local.microsecond / 1000):03d} UTC+3"
                )
            return local.strftime("%Y-%m-%d %H:%M:%S") + " UTC+3"
        except ValueError:
            continue
    return raw


def _setup_server_logging() -> None:
    log_dir = BASE / "user_data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pair-config.log"

    class _UtcPlus3Formatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):  # noqa: N802
            dt = datetime.fromtimestamp(record.created, tz=APP_TZ)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    fmt = _UtcPlus3Formatter(
        "%(asctime)s UTC+3 - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _server_log.setLevel(logging.INFO)
    if not _server_log.handlers:
        fh = RotatingFileHandler(
            log_file, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        _server_log.addHandler(fh)
    # Child loggers (adaptive_scan, …) share the same file without duplicate handlers.
    _server_log.propagate = False
    logging.getLogger("pair_config.adaptive_scan").setLevel(logging.INFO)


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


def test_strategy_settings_file() -> Path:
    return tc.user_data_dir(BASE) / "test_strategy_settings.json"


def strategy_ui_placement_file() -> Path:
    return tc.user_data_dir(BASE) / "strategy_ui_placement.json"


DEFAULT_TEST_STRATEGY_SETTINGS: dict[str, Any] = {
    "max_open_trades": 8,
    "max_open_trades_per_strategy": 2,
    "stake_amount": 5.0,
    "stoploss": -0.02,
    "take_profit": 0.02,
}
MIN_TEST_TAKE_PROFIT = 0.005
MAX_TEST_TAKE_PROFIT = 0.50
MIN_TEST_STOPLOSS = -0.20
MAX_TEST_STOPLOSS = -0.01


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
# No hard cap: user may set any non-negative int. float("inf") from configs → this sentinel for JSON/int APIs.
INF_MAX_OPEN_TRADES = 1_000_000
MIN_STAKE_AMOUNT = 1
MAX_STAKE_AMOUNT = 100

from htf1h_catalog import (  # noqa: E402
    HTF1H_IDS,
    HTF1H_SPECS,
    SL_CHOICES,
    TP_CHOICES,
    default_sltp,
    snap_sl_pct,
    snap_tp_pct,
)

AVAILABLE_STRATEGIES = [
    {
        "id": "PsaraFlipStrategy",
        "num": 1,
        "ui_order": 1,
        "name": "Parabolic SAR flip",
        "desc": "ML pack · sim new_psar · test ML PnL 87.1 USDT · SL -2.0% · TP 2.2% · gate>=45% · exp exp14_lgbm_gate045",
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
        "id": "HeikinAshiFlipStrategy",
        "num": 13,
        "ui_order": 13,
        "name": "Heikin Ashi flip",
        "desc": "ML pack · sim chart_ha · test ML PnL 57.2 USDT · SL -1.6% · TP 1.1% · gate>=45% · exp exp14_lgbm_gate045",
    },
    {
        "id": "HmaPpoAtrComboStrategy",
        "num": 20,
        "ui_order": 20,
        "name": "HMA+PPO+ATR",
        "desc": "ML pack · sim combo_hma_ppo_atr · test ML PnL 45.5 USDT · SL -1.8% · TP 1.4% · gate>=45% · exp exp14_lgbm_gate045",
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
        "id": "FibPullbackStrategy",
        "num": 39,
        "ui_order": 38,
        "name": "Fib pullback (DCA)",
        "desc": "ML pack · sim trend_fib · test ML PnL 15.3 USDT · SL -3.0% · TP 3.5% · gate>=45% · legacy_april_cut",
    },
]


# Dropped from the live catalog. Names stay so closed-trade history still labels them.
RETIRED_STRATEGY_LABELS = [
    {
        "id": "AltVolumeBreakoutStrategy",
        "num": 31,
        "ui_order": 0,
        "name": "Alt volume breakout",
        "desc": "ML pack · sim scalp_liq_breakout · test ML PnL 135.3 USDT · SL -1.5% · TP 1.2% · gate>=55% · exp exp31_mlp",
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
        "id": "PsaraFlipTestStrategy",
        "num": 101,
        "ui_order": 101,
        "name": "Parabolic SAR flip (test)",
        "test_group": True,
        "desc": "Тест · long-only · chase RSI≤60 / range≤65% · gate≥65% · SL/TP из test settings",
    },
    {
        "id": "AtrChannelBreakoutTestStrategy",
        "num": 102,
        "ui_order": 102,
        "name": "ATR channel breakout (test)",
        "test_group": True,
        "desc": "Тест · wide XGB+sigmoid · scenario chart3_atrch_test · gate из meta · те же сигналы что #2",
    },
    {
        "id": "AdxMomentumTestStrategy",
        "num": 103,
        "ui_order": 103,
        "name": "Breakout-Retest (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход если close теряет EMA20",
    },
    {
        "id": "SupertrendTestStrategy",
        "num": 104,
        "ui_order": 104,
        "name": "Supertrend (ATR) (test)",
        "test_group": True,
        "desc": "Тест · long-only · SL −1.2% · DI+/bull bar · ST dynamic SL · block hot UTC · gate ≥90%",
    },
    {
        "id": "CmfZeroCrossTestStrategy",
        "num": 105,
        "ui_order": 105,
        "name": "CMF zero cross (test)",
        "test_group": True,
        "desc": "Тест · long-only · chase RSI≤60 · SL/TP test settings · выход cmf_flip · gate≥65%",
    },
    {
        "id": "ScalpEmaCrossTestStrategy",
        "num": 106,
        "ui_order": 106,
        "name": "Scalp EMA 8/21 (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход ema_flip · live #4 · gate>=55%",
    },
    {
        "id": "ChaikinOscTestStrategy",
        "num": 107,
        "ui_order": 107,
        "name": "Chaikin Oscillator (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход adosc_flip · live #5 · gate>=45%",
    },
    {
        "id": "DonchianBreakoutTestStrategy",
        "num": 108,
        "ui_order": 108,
        "name": "Donchian / Turtle (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход don_mid · live #6 · gate>=45%",
    },
    {
        "id": "PpoSignalTestStrategy",
        "num": 109,
        "ui_order": 109,
        "name": "PPO signal cross (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход ppo_flip · live #7 · gate>=55%",
    },
    {
        "id": "DonchianAdxVolComboTestStrategy",
        "num": 110,
        "ui_order": 110,
        "name": "Donchian+ADX+Vol (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход don_mid · live #8 · gate>=65%",
    },
    {
        "id": "ObvEmaCrossTestStrategy",
        "num": 111,
        "ui_order": 111,
        "name": "OBV EMA cross (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход obv_flip · live #9 · gate>=45%",
    },
    {
        "id": "ElderRayTestStrategy",
        "num": 112,
        "ui_order": 112,
        "name": "Elder Ray Bull/Bear (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход elder_flip · live #10 · gate>=45%",
    },
    {
        "id": "AltVolumeBreakoutTestStrategy",
        "num": 113,
        "ui_order": 113,
        "name": "Alt volume breakout (test)",
        "test_group": True,
        "desc": "Тест · long-only · chase RSI≤60 · SL/TP test settings · выход don_mid · gate≥65%",
    },
    {
        "id": "BollingerRsiTestStrategy",
        "num": 114,
        "ui_order": 114,
        "name": "Mean-reversion (BB) (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход bb_mid · live #33 · gate>=70%",
    },
    {
        "id": "MacdEmaTestStrategy",
        "num": 115,
        "ui_order": 115,
        "name": "MACD + EMA200 (test)",
        "test_group": True,
        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход macd_flip · live #34 · gate>=45%",
    },
    {
        "id": "GruBarrierStrategy",
        "num": 116,
        "ui_order": 116,
        "name": "GRU seq-gate (EMA)",
        "test_group": True,
        "desc": "Тест · EMA 8/21 + GRU strategy-gate (seq_gate_compare #1) · thr≈85% · SL −1% · TP 0.8% · без LightGBM",
    },
    {
        "id": "OkalaRange8020Strategy",
        "num": 117,
        "ui_order": 117,
        "name": "Okala 80/20 range (test)",
        "test_group": True,
        "desc": "Тест · rejection/continuation на 20%/80% range · SL −1% · TP 1.5% · ML gate≥55% · sim pro_okala_8020 · ML test +21.1 USDT",
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
        "id": "CriptoPairsStrategy",
        "num": None,
        "ui_order": 1000,
        "name": "CriptoPairs",
        "desc": "Legacy · off by default. Kept in catalog for history/stats labels.",
    },
]

for _htf in HTF1H_SPECS:
    AVAILABLE_STRATEGIES.append(
        {
            "id": _htf["id"],
            "num": _htf["num"],
            "ui_order": _htf["num"],
            "name": f"{_htf['name']} · 1-часовые",
            "htf_group": True,
            "desc": (
                f"1h · стоп −{_htf['sl']}% · тейк +{_htf['tp']}% · "
                "сигнал по закрытой часовой свече · ML gate как у основной"
            ),
        }
    )
del _htf


# Birth group from catalog (static). Runtime panel may differ via strategy_ui_placement.json.
CATALOG_TEST_IDS = frozenset(
    s["id"] for s in AVAILABLE_STRATEGIES if s.get("test_group")
)
# Backward-compat alias; prefer effective_ui_panel() / is_effective_test_strategy().
TEST_STRATEGY_IDS = CATALOG_TEST_IDS

_PLACEMENT_NOTE = (
    "main = Strategies panel; not in main and not in hidden = Тестовые; "
    "hidden = neither (kept for history labels / sim)."
)


def _catalog_birth_test(sid: str) -> bool:
    return sid in CATALOG_TEST_IDS


def _default_hidden_ids() -> list[str]:
    return [s["id"] for s in AVAILABLE_STRATEGIES if not s.get("test_group")]


def _normalize_placement(data: dict[str, Any] | None) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    main: list[str] = []
    seen_main: set[str] = set()
    for x in raw.get("main") or []:
        sid = str(x).strip()
        if sid and sid not in seen_main:
            main.append(sid)
            seen_main.add(sid)
    hidden: list[str] = []
    seen_hidden: set[str] = set()
    for x in raw.get("hidden") or []:
        sid = str(x).strip()
        if sid and sid not in seen_hidden and sid not in seen_main:
            hidden.append(sid)
            seen_hidden.add(sid)
    out = {
        "main": main,
        "hidden": hidden,
        "_note": str(raw.get("_note") or _PLACEMENT_NOTE),
    }
    if "_hidden_disabled" in raw:
        out["_hidden_disabled"] = bool(raw.get("_hidden_disabled"))
    else:
        out["_hidden_disabled"] = True
    return out


def save_strategy_ui_placement(data: dict[str, Any]) -> dict[str, Any]:
    """Persist per-user placement into control_plane.sqlite (+ JSON cache)."""
    uid = user_prefs.settings_owner_id()
    normalized = _normalize_placement(data)
    if "_hidden_disabled" in data:
        normalized["_hidden_disabled"] = bool(data.get("_hidden_disabled"))
    return user_prefs.save_placement(uid, normalized, base=BASE)


def ensure_strategy_ui_placement() -> dict[str, Any]:
    """Load per-user placement from DB (migrate JSON once; default all→test)."""
    uid = user_prefs.settings_owner_id()
    return user_prefs.load_placement(uid, base=BASE)


def effective_ui_panel(sid: str, placement: dict[str, Any] | None = None) -> str:
    """Return 'main' | 'htf1h' | 'test' | 'hidden'.

    1h strategies stay on the 1h panel. placement.main only marks them as
    «основные» for that user. Regular strategies use main vs test the same way.
    """
    p = placement if placement is not None else ensure_strategy_ui_placement()
    main = set(p.get("main") or [])
    hidden = set(p.get("hidden") or [])
    if sid in hidden:
        return "hidden"
    if sid in HTF1H_IDS:
        return "htf1h"
    if sid in main:
        return "main"
    return "test"


def is_effective_test_strategy(sid: str, placement: dict[str, Any] | None = None) -> bool:
    return effective_ui_panel(sid, placement) == "test"


def ui_placement_payload(placement: dict[str, Any] | None = None) -> dict[str, Any]:
    p = placement if placement is not None else ensure_strategy_ui_placement()
    catalog_ids = {s["id"] for s in AVAILABLE_STRATEGIES}
    test_ids = [
        s["id"]
        for s in AVAILABLE_STRATEGIES
        if effective_ui_panel(s["id"], p) == "test"
    ]
    main_ids = [sid for sid in (p.get("main") or []) if sid in catalog_ids]
    htf_ids = [
        s["id"]
        for s in AVAILABLE_STRATEGIES
        if effective_ui_panel(s["id"], p) == "htf1h"
    ]
    return {
        "main": main_ids,
        "test": test_ids,
        "htf1h": htf_ids,
        "hidden": list(p.get("hidden") or []),
        "hidden_count": len(p.get("hidden") or []),
    }


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
    raw = Path(name)
    candidates = [
        raw if raw.is_absolute() else (BASE / name),
        BASE / "user_data" / name,
        cfg_path.parent / raw.name,
    ]
    for candidate in candidates:
        try:
            db_path = candidate.resolve()
        except OSError:
            continue
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
          AND cd_key IN (
            'ml_confidence', 'ml_gate_confidence', 'ml_predicted', 'ml_min_confidence',
            'take_profit_ratio', 'stop_loss_ratio'
          )
    """
    out: dict[int, dict[str, Any]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            # Local trade_executor DBs may predate this table — create if missing.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_custom_data (
                    id INTEGER NOT NULL PRIMARY KEY,
                    ft_trade_id INTEGER NOT NULL,
                    cd_key VARCHAR(255) NOT NULL,
                    cd_type VARCHAR(25) NOT NULL,
                    cd_value TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME,
                    UNIQUE (ft_trade_id, cd_key)
                )
                """
            )
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
        msg = str(exc).lower()
        if "no such table" in msg:
            _server_log.debug("trade ml meta %s: %s", bot, exc)
        else:
            _server_log.warning("trade ml meta %s: %s", bot, exc)
    return out


def get_trade_ml_meta_payload(bot: str, trade_ids: list[int]) -> dict[str, Any]:
    meta = load_trade_ml_meta(bot, trade_ids)
    return {"bot": bot, "meta": {str(k): v for k, v in meta.items()}}


def _trade_row_id(trade: dict[str, Any]) -> int | None:
    raw = trade.get("trade_id", trade.get("id"))
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def attach_trade_ml_meta(bot: str, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy trades and attach ml_meta from sqlite (idempotent if already present)."""
    if not trades:
        return []
    bot = resolve_bot(bot)
    ids: list[int] = []
    for t in trades:
        tid = _trade_row_id(t)
        if tid is not None and not (isinstance(t.get("ml_meta"), dict) and t["ml_meta"]):
            ids.append(tid)
    meta_by_id = load_trade_ml_meta(bot, ids) if ids else {}
    out: list[dict[str, Any]] = []
    for t in trades:
        row = dict(t)
        tid = _trade_row_id(row)
        if tid is not None and "trade_id" not in row:
            row["trade_id"] = tid
        existing = row.get("ml_meta") if isinstance(row.get("ml_meta"), dict) else {}
        extra = meta_by_id.get(tid or -1) or {}
        if existing or extra:
            row["ml_meta"] = {**existing, **extra}
        out.append(row)
    return out


def _row_optional_float(row: sqlite3.Row, key: str) -> float | None:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _closed_trade_row_to_json(row: sqlite3.Row, ml: dict[str, Any]) -> dict[str, Any]:
    close_profit = row["close_profit"]
    close_pct = round(float(close_profit) * 100, 2) if close_profit is not None else None
    amount = None
    leverage = None
    try:
        if row["amount"] is not None:
            amount = float(row["amount"])
    except (KeyError, IndexError, TypeError, ValueError):
        amount = None
    try:
        if row["leverage"] is not None:
            leverage = float(row["leverage"])
    except (KeyError, IndexError, TypeError, ValueError):
        leverage = None
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
        "amount": amount,
        "leverage": leverage,
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
    stop_ratio = _row_optional_float(row, "initial_stop_loss_pct")
    if stop_ratio is None:
        stop_ratio = _row_optional_float(row, "stop_loss_pct")
    if stop_ratio is None and ml and ml.get("stop_loss_ratio") is not None:
        try:
            stop_ratio = float(ml["stop_loss_ratio"])
        except (TypeError, ValueError):
            stop_ratio = None
    if stop_ratio is not None:
        trade["initial_stop_loss_pct"] = stop_ratio
    if ml and ml.get("take_profit_ratio") is not None:
        try:
            trade["take_profit_ratio"] = float(ml["take_profit_ratio"])
        except (TypeError, ValueError):
            pass
    if ml:
        trade["ml_meta"] = ml
    return trade


# Hard safety cap for closed-trade dumps (UI history / stats "all time").
CLOSED_TRADES_HARD_CAP = 50000


def _trade_is_llm(enter_tag: Any, strategy: Any) -> bool:
    tag = str(enter_tag or "")
    strat = str(strategy or "")
    return tag.startswith("DeepseekLlmStrategy") or strat == "DeepseekLlmStrategy"


def _trade_is_book(enter_tag: Any, strategy: Any) -> bool:
    tag = str(enter_tag or "")
    strat = str(strategy or "")
    return tag.startswith("Deepseek50") or strat == "Deepseek50"


def _trade_is_cursor(enter_tag: Any, strategy: Any) -> bool:
    tag = str(enter_tag or "")
    strat = str(strategy or "")
    return tag.startswith("CursorLlmStrategy") or strat == "CursorLlmStrategy"


def _trade_is_cursor_book(enter_tag: Any, strategy: Any) -> bool:
    tag = str(enter_tag or "")
    strat = str(strategy or "")
    return tag.startswith("Cursor50") or strat == "Cursor50"


def _trade_is_model_bot(enter_tag: Any, strategy: Any) -> bool:
    return (
        _trade_is_llm(enter_tag, strategy)
        or _trade_is_book(enter_tag, strategy)
        or _trade_is_cursor(enter_tag, strategy)
        or _trade_is_cursor_book(enter_tag, strategy)
    )


def _closed_trade_strategy_id(enter_tag: Any, strategy: Any) -> str:
    """Same id the UI uses to put a trade on the main or test card."""
    raw = str(enter_tag or "").strip()
    if not raw or raw == "MultiStrategyRouter":
        raw = str(strategy or "").strip()
    if raw == "MultiStrategyRouter":
        raw = ""
    while raw.endswith(":inv") or raw.endswith(":hedge"):
        raw = raw.rsplit(":", 1)[0]
    return raw


def closed_profit_from_db(bot: str) -> dict[str, Any]:
    """Net closed PnL from sqlite.

    Signal-engine /profit is empty on the shared stack. History and stats already
    subtract the Bybit 0.1%×2 fee; the main-page card must use that same net.
    Strategy trades are split the way the two cards split open trades: test panel
    vs everything else (main + hidden).
    """
    bot = resolve_bot(bot)
    empty = {
        "profit_closed_coin": 0.0,
        "profit_closed_fiat": 0.0,
        "profit_closed_main_coin": 0.0,
        "profit_closed_test_coin": 0.0,
        "profit_closed_htf_coin": 0.0,
        "trade_count": 0,
        "trade_count_main": 0,
        "trade_count_test": 0,
        "trade_count_htf": 0,
        "source": "db",
    }
    if bot not in CONFIGS:
        return empty
    db_path = _bot_db_path(bot)
    if not db_path:
        return empty
    query = """
        SELECT enter_tag, strategy, close_profit_abs, realized_profit,
               amount, open_rate, close_rate, stake_amount, leverage
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT enter_tag, strategy, close_profit_abs, realized_profit
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    try:
        with sqlite3.connect(db_path) as conn:
            try:
                rows = conn.execute(query).fetchall()
            except sqlite3.Error:
                rows = conn.execute(query_legacy).fetchall()
    except sqlite3.Error as exc:
        _server_log.warning("closed profit %s: %s", bot, exc)
        return empty

    placement = None
    if bot == "strategy":
        try:
            placement = ensure_strategy_ui_placement()
        except Exception:
            placement = None

    total = main = test = htf = 0.0
    n_main = n_test = n_htf = 0
    for row in rows:
        enter_tag, strategy, profit_abs = row[0], row[1], row[2]
        realized = row[3] if len(row) > 3 else None
        amount = row[4] if len(row) > 4 else None
        open_rate = row[5] if len(row) > 5 else None
        close_rate = row[6] if len(row) > 6 else None
        stake_amount = row[7] if len(row) > 7 else None
        leverage = row[8] if len(row) > 8 else None
        try:
            gross = float(profit_abs if profit_abs is not None else realized or 0.0)
        except (TypeError, ValueError):
            gross = 0.0
        pnl = _pnl_net_of_fees(
            gross,
            amount=amount,
            open_rate=open_rate,
            close_rate=close_rate,
            stake_amount=stake_amount,
            leverage=leverage,
        )
        if bot == "strategy" and _trade_is_model_bot(enter_tag, strategy):
            continue
        total += pnl
        sid = _closed_trade_strategy_id(enter_tag, strategy) if bot == "strategy" else ""
        if bot == "strategy" and sid in HTF1H_IDS:
            htf += pnl
            n_htf += 1
        elif bot == "strategy" and sid and placement is not None and is_effective_test_strategy(sid, placement):
            test += pnl
            n_test += 1
        else:
            main += pnl
            n_main += 1
    return {
        "profit_closed_coin": round(total, 6),
        "profit_closed_fiat": round(total, 6),
        "profit_closed_main_coin": round(main, 6),
        "profit_closed_test_coin": round(test, 6),
        "profit_closed_htf_coin": round(htf, 6),
        "trade_count": n_main + n_test + n_htf,
        "trade_count_main": n_main,
        "trade_count_test": n_test,
        "trade_count_htf": n_htf,
        "source": "db",
    }


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
               amount, leverage, stake_amount, close_profit, close_profit_abs,
               exit_reason, realized_profit,
               initial_stop_loss_pct, stop_loss_pct
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
        ORDER BY close_date DESC
        LIMIT ?
    """
    # Older DBs may lack amount/leverage columns — fall back without them.
    query_legacy = """
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
            try:
                rows = conn.execute(query, (limit,)).fetchall()
            except sqlite3.Error:
                rows = conn.execute(query_legacy, (limit,)).fetchall()
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
    with maybe_admin_trade_context() as mirrored:
        if int(limit) > 0:
            limit = max(1, min(int(limit), CLOSED_TRADES_HARD_CAP))
        if bot:
            bot = resolve_bot(bot)
            if bot not in CONFIGS:
                return {"error": "invalid bot"}
            return {
                "bot": bot,
                "trades": load_closed_trades_from_db(bot, limit),
                "_demo_mirror_admin": bool(mirrored),
            }
        out: dict[str, Any] = {
            name: load_closed_trades_from_db(name, limit) for name in CONFIGS
        }
        out["_demo_mirror_admin"] = bool(mirrored)
        return out


def _bot_api_v1_url(bot: str, path: str) -> str:
    ports = tc.resolve_bot_ports()
    port = ports.get(bot)
    if not port:
        raise KeyError(bot)
    return f"http://127.0.0.1:{port}/api/v1/{path.lstrip('/')}"


def _open_trades_backoff_key(bot: str, tenant_key: str | None = None) -> str:
    return f"{tenant_key or _stats_tenant_key()}:{resolve_bot(bot)}"


def _fetch_bot_open_trades_live(
    bot: str, *, timeout: float = 1.5, tenant_key: str | None = None
) -> list[dict[str, Any]] | None:
    """Live open trades from bot API; None if bot is down / timed out."""
    bot = resolve_bot(bot)
    key = tenant_key or _stats_tenant_key()
    backoff_key = _open_trades_backoff_key(bot, key)
    now = time.monotonic()
    until = _open_trades_live_backoff.get(backoff_key, 0.0)
    if until > now:
        return None
    try:
        url = _bot_api_v1_url(bot, "status")
        req = urllib.request.Request(url, headers={"Authorization": _basic_header()}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"null")
        if isinstance(data, list):
            _open_trades_live_backoff.pop(backoff_key, None)
            remember_open_trades(bot, data, source="live", tenant_key=key)
            # Return the cached enriched copy (with ml_meta) when available.
            cached = get_cached_open_trades(bot, max_age=None, tenant_key=key)
            if cached and isinstance(cached.get("trades"), list):
                return cached["trades"]
            return attach_trade_ml_meta(bot, data)
        return None
    except Exception:  # noqa: BLE001
        # ctbot wedged — stop hammering for a bit (UI uses cache/db).
        _open_trades_live_backoff[backoff_key] = time.monotonic() + 30.0
        return None


def load_open_trades_from_db(bot: str, *, llm_mode: str = "exclude") -> list[dict[str, Any]]:
    """Open trades from sqlite with mark-to-market PnL for the UI.

    trade_executor inserts close_profit=0 on entry — that must not freeze UI at 0%.
    Always reprice from public tickers when possible.
    Includes current_rate / stop / min-max so DB fallback matches live card fields
    (avoids flicker between rich /status and sparse sqlite rows).
    """
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        return []
    db_path = _bot_db_path(bot)
    if not db_path:
        return []
    query = """
        SELECT id, pair, strategy, enter_tag, is_short,
               open_date, open_rate, stake_amount, max_stake_amount, amount, leverage,
               close_profit, close_profit_abs, realized_profit,
               stop_loss, stop_loss_pct, min_rate, max_rate,
               liquidation_price, funding_fees
        FROM trades
        WHERE is_open = 1
        ORDER BY id DESC
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        marks: dict[str, float] = {}
        try:
            from user_exchange import fetch_public_klines, ft_pair_to_symbol
        except Exception:  # noqa: BLE001
            fetch_public_klines = None  # type: ignore[assignment]
            ft_pair_to_symbol = None  # type: ignore[assignment]

        def _mark_for(pair: str) -> float | None:
            if not fetch_public_klines or not ft_pair_to_symbol:
                return None
            if pair in marks:
                val = marks[pair]
                return None if val != val else val
            try:
                sym = ft_pair_to_symbol(str(pair))
                tick_rows = fetch_public_klines(sym, interval="1", limit=1)
                if tick_rows:
                    marks[pair] = float(tick_rows[-1][4])
                    return marks[pair]
            except Exception:  # noqa: BLE001
                marks[pair] = float("nan")
            return None

        out: list[dict[str, Any]] = []
        for r in rows:
            stake = float(r["stake_amount"] or 0)
            stored_ratio = r["close_profit"]
            stored_abs = r["close_profit_abs"]
            profit_abs = float(stored_abs) if stored_abs is not None else None
            profit_pct = (
                float(stored_ratio) * 100.0 if stored_ratio is not None else None
            )
            open_rate = float(r["open_rate"] or 0)
            lev = float(r["leverage"] or 1) or 1.0
            is_short = bool(r["is_short"])
            mark: float | None = None
            if open_rate > 0:
                mark = _mark_for(str(r["pair"]))
                if mark is not None and mark > 0:
                    if is_short:
                        pr = ((open_rate - mark) / open_rate) * lev
                    else:
                        pr = ((mark - open_rate) / open_rate) * lev
                    profit_abs = pr * stake
                    profit_pct = pr * 100.0
            if profit_abs is None:
                profit_abs = 0.0

            stop_abs = r["stop_loss"]
            stop_ratio = r["stop_loss_pct"]
            if stop_ratio is not None:
                try:
                    stop_ratio = float(stop_ratio)
                except (TypeError, ValueError):
                    stop_ratio = None
            stoploss_dist = None
            stoploss_dist_ratio = None
            if mark is not None and mark > 0 and stop_abs is not None:
                try:
                    stop_f = float(stop_abs)
                    stoploss_dist = (mark - stop_f) if not is_short else (stop_f - mark)
                    if open_rate > 0:
                        stoploss_dist_ratio = stoploss_dist / open_rate
                except (TypeError, ValueError):
                    pass

            max_stake = r["max_stake_amount"]
            try:
                max_stake_f = float(max_stake) if max_stake is not None else stake
            except (TypeError, ValueError):
                max_stake_f = stake

            is_llm = _trade_is_llm(r["enter_tag"], r["strategy"])
            is_book = _trade_is_book(r["enter_tag"], r["strategy"])
            is_cursor = _trade_is_cursor(r["enter_tag"], r["strategy"])
            is_cursor_book = _trade_is_cursor_book(r["enter_tag"], r["strategy"])
            if bot == "strategy" and llm_mode == "exclude" and (
                is_llm or is_book or is_cursor or is_cursor_book
            ):
                continue
            if bot == "strategy" and llm_mode == "only" and not is_llm:
                continue
            if bot == "strategy" and llm_mode == "book" and not is_book:
                continue
            if bot == "strategy" and llm_mode == "cursor" and not is_cursor:
                continue
            if bot == "strategy" and llm_mode == "cursor50" and not is_cursor_book:
                continue

            out.append(
                {
                    "bot": bot,
                    "trade_id": int(r["id"]),
                    "pair": r["pair"],
                    "strategy": r["strategy"],
                    "enter_tag": r["enter_tag"],
                    "is_short": is_short,
                    "open_date": r["open_date"],
                    "open_rate": r["open_rate"],
                    "current_rate": mark,
                    "amount": r["amount"],
                    "leverage": lev,
                    "stake_amount": stake,
                    "max_stake_amount": max_stake_f,
                    "profit_abs": profit_abs,
                    "profit_pct": profit_pct,
                    "total_profit_abs": profit_abs,
                    "total_profit_ratio": (profit_pct / 100.0) if profit_pct is not None else None,
                    "stop_loss_abs": float(stop_abs) if stop_abs is not None else None,
                    "stop_loss_ratio": stop_ratio,
                    "stoploss_current_dist": stoploss_dist,
                    "stoploss_current_dist_ratio": stoploss_dist_ratio,
                    "min_rate": r["min_rate"],
                    "max_rate": r["max_rate"],
                    "liquidation_price": r["liquidation_price"],
                    "funding_fees": r["funding_fees"],
                    "orders": [],
                }
            )
        return attach_trade_ml_meta(bot, out)
    except sqlite3.Error as exc:
        _server_log.warning("open trades %s: %s", bot, exc)
        return []


# --- Open trades status cache (full /status payloads for UI) ---
OPEN_TRADES_WARM_SEC = 12.0
OPEN_TRADES_STALE_SEC = 180.0
_open_trades_lock = threading.RLock()
# tenant_key -> bot -> {trades, ts, source}
_open_trades_cache: dict[str, dict[str, dict[str, Any]]] = {}
# bot -> monotonic deadline; skip live probes while ctbot is wedged
_open_trades_live_backoff: dict[str, float] = {}


def remember_open_trades(
    bot: str,
    trades: list[dict[str, Any]],
    *,
    source: str = "live",
    tenant_key: str | None = None,
) -> None:
    bot = resolve_bot(bot)
    key = tenant_key or _stats_tenant_key()
    enriched = attach_trade_ml_meta(bot, list(trades))
    with _open_trades_lock:
        bucket = _open_trades_cache.setdefault(key, {})
        prev = bucket.get(bot) or {}
        prev_trades = list(prev.get("trades") or [])
        # Empty live/cache must not erase sqlite-backed opens (shared signal-engine).
        if (
            not enriched
            and prev_trades
            and str(source).startswith("live")
        ):
            return
        bucket[bot] = {
            "trades": enriched,
            "ts": time.time(),
            "source": source,
        }


def get_cached_open_trades(
    bot: str,
    *,
    max_age: float | None = OPEN_TRADES_STALE_SEC,
    tenant_key: str | None = None,
) -> dict[str, Any] | None:
    bot = resolve_bot(bot)
    key = tenant_key or _stats_tenant_key()
    with _open_trades_lock:
        entry = (_open_trades_cache.get(key) or {}).get(bot)
        if not entry:
            return None
        age = time.time() - float(entry.get("ts") or 0)
        if max_age is not None and age > max_age:
            return None
        return {
            "trades": list(entry.get("trades") or []),
            "age_sec": round(age, 1),
            "source": entry.get("source") or "cache",
            "ts": entry.get("ts"),
        }


def _open_trades_prefer_db_if_live_empty(
    bot: str,
    live: list[dict[str, Any]] | None,
    *,
    tenant_key: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Shared signal-engine /status is often empty while executor sqlite still has opens.

    Prefer fresh sqlite (mark PnL) over stale warm-cache zeros from entry-time close_profit=0.
    """
    db_trades = load_open_trades_from_db(bot)
    if live:
        # Strategy shared stack: SE /status is empty of fills — always use executor DB.
        if bot == "strategy" and db_trades:
            remember_open_trades(bot, db_trades, source="db", tenant_key=tenant_key)
            return db_trades, "db"
        return live, "live"
    if db_trades:
        remember_open_trades(bot, db_trades, source="db", tenant_key=tenant_key)
        return db_trades, "db"
    if live is None:
        cached = get_cached_open_trades(bot, max_age=None, tenant_key=tenant_key)
        cached_trades = list((cached or {}).get("trades") or [])
        if cached_trades:
            return cached_trades, f"cache:{cached.get('source') or 'live'}"
    remember_open_trades(bot, [], source="db", tenant_key=tenant_key)
    return [], "db"


def refresh_open_trades_cache_for_bots(
    bots: list[str] | None = None,
    *,
    tenant_key: str | None = None,
) -> dict[str, Any]:
    """Pull live /status (short timeout) or fall back to last cache / sqlite."""
    key = tenant_key or _stats_tenant_key()
    out: dict[str, Any] = {}
    for bot in bots or list(CONFIGS):
        timeout = 0.6 if bot == "finder" else 2.0
        live = _fetch_bot_open_trades_live(bot, timeout=timeout, tenant_key=key)
        trades, source = _open_trades_prefer_db_if_live_empty(
            bot, live, tenant_key=key
        )
        cached = get_cached_open_trades(bot, max_age=None, tenant_key=key) or {}
        out[bot] = {
            "trades": trades,
            "age_sec": 0.0 if source in ("live", "db") else float(cached.get("age_sec") or 0),
            "source": source,
            "ts": cached.get("ts") or time.time(),
        }
    return out


def get_open_trades_bundle(*, refresh: bool = False) -> dict[str, Any]:
    """Open trades payload for the current tenant.

    Strategy (shared SE): always sqlite + mark PnL — /status has no fills.
    Finder/Grid: prefer fresh live /status (full card fields); enriched DB fallback.
    refresh=1 also kicks a background live pull.
    Demo tenants mirror admin house trades (read-only in UI).
    """
    with maybe_admin_trade_context() as mirrored:
        key = _stats_tenant_key()
        if refresh:
            threading.Thread(
                target=lambda: refresh_open_trades_cache_for_bots(tenant_key=key),
                name=f"open-trades-refresh-{key}",
                daemon=True,
            ).start()

        bots: dict[str, Any] = {}
        for bot in CONFIGS:
            # Finder/Grid ctbot /status is authoritative — do not clobber with sparse DB
            # every poll (that caused current_rate/SL flickering in the UI).
            if bot in ("finder", "grid"):
                cached = get_cached_open_trades(
                    bot, max_age=OPEN_TRADES_STALE_SEC, tenant_key=key
                )
                cached_trades = list((cached or {}).get("trades") or [])
                src = str((cached or {}).get("source") or "")
                if cached_trades and (
                    src.startswith("live") or src.startswith("cache:live")
                ):
                    entry = dict(cached)
                    entry["closed_profit"] = closed_profit_from_db(bot)
                    bots[bot] = entry
                    continue

            db_trades = load_open_trades_from_db(bot)
            if db_trades:
                remember_open_trades(bot, db_trades, source="db", tenant_key=key)
                entry = {
                    "trades": db_trades,
                    "age_sec": 0.0,
                    "source": "db",
                    "ts": time.time(),
                }
            else:
                cached = get_cached_open_trades(bot, max_age=None, tenant_key=key)
                cached_trades = list((cached or {}).get("trades") or [])
                # Strategy fills live only in executor DB — never keep non-DB cache opens.
                if cached_trades and bot != "strategy":
                    entry = dict(cached)
                else:
                    remember_open_trades(bot, [], source="db", tenant_key=key)
                    entry = {
                        "trades": [],
                        "age_sec": 0.0,
                        "source": "db",
                        "ts": time.time(),
                    }
            entry["closed_profit"] = closed_profit_from_db(bot)
            bots[bot] = entry
        return {
            "bots": bots,
            "built_at": int(time.time() * 1000),
            "cached": True,
            "demo_mirror_admin": bool(mirrored),
            "read_only": bool(mirrored),
        }


def start_open_trades_cache_scheduler() -> None:
    """Keep open-trade lists warm so UI is not blocked on slow ctbot /status."""

    def _loop() -> None:
        time.sleep(2)
        while True:
            try:
                admin_user = {"id": "admin", "role": "admin", "username": "admin"}
                token = tc.set_request_user(admin_user)
                try:
                    refresh_open_trades_cache_for_bots(tenant_key="admin")
                finally:
                    tc.reset_request_user(token)
                with _open_trades_lock:
                    other_keys = [k for k in _open_trades_cache if k != "admin"]
                for key in other_keys:
                    if not key.startswith("user:"):
                        continue
                    uid = key.split(":", 1)[1]
                    u = tm.get_user_by_id(uid)
                    if not u:
                        continue
                    token = tc.set_request_user(u)
                    try:
                        refresh_open_trades_cache_for_bots(tenant_key=key)
                    finally:
                        tc.reset_request_user(token)
            except Exception as exc:  # noqa: BLE001
                _server_log.warning("open-trades warmer: %s", exc)
            time.sleep(OPEN_TRADES_WARM_SEC)

    threading.Thread(target=_loop, name="open-trades-warmer", daemon=True).start()
    _server_log.info(
        "open-trades cache warmer started (every %ss, stale<=%ss)",
        int(OPEN_TRADES_WARM_SEC),
        int(OPEN_TRADES_STALE_SEC),
    )


# Auto position reconcile (ghosts vs Bybit) — same path as UI «Синхронизировать».
# Default 5m: Finder/Strategy share one Bybit account; ghosts appear within minutes.
POSITION_RECONCILE_INTERVAL_SEC = float(
    os.environ.get("CT_POSITION_RECONCILE_SEC", str(5 * 60))
)
_position_reconcile_lock = threading.Lock()
_position_reconcile_last: dict[str, Any] = {
    "ts": None,
    "ok": None,
    "ghost_count_before": None,
    "ghost_count_after": None,
    "fixed": 0,
    "error": None,
}


def run_position_reconcile_auto(*, force: bool = False) -> dict[str, Any]:
    """Scan ghosts and archive/forceexit them. Safe to call often (no-op if clean)."""
    global _position_reconcile_last
    if not _position_reconcile_lock.acquire(blocking=force):
        return {"ok": False, "skipped": True, "reason": "busy"}
    try:
        before = reconcile_positions(BASE)
        ghosts = int(before.get("ghost_count") or 0)
        dupes = int(before.get("duplicate_count") or len(before.get("duplicates") or []))
        if ghosts <= 0 and dupes <= 0:
            _position_reconcile_last = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ok": True,
                "ghost_count_before": 0,
                "ghost_count_after": 0,
                "duplicate_count_before": 0,
                "duplicate_count_after": 0,
                "fixed": 0,
                "error": None,
                "duplicate_pairs": before.get("duplicate_pairs") or [],
            }
            return {"ok": True, "fixed": 0, "before": before, "after": before}

        result = fix_reconcile(BASE)
        after = result.get("after") or {}
        fixed_n = len(result.get("fixed") or [])
        _position_reconcile_last = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ok": bool(result.get("ok")),
            "ghost_count_before": ghosts,
            "ghost_count_after": int(after.get("ghost_count") or 0),
            "duplicate_count_before": dupes,
            "duplicate_count_after": int(
                after.get("duplicate_count") or len(after.get("duplicates") or [])
            ),
            "fixed": fixed_n,
            "error": None,
            "duplicate_pairs": after.get("duplicate_pairs") or before.get("duplicate_pairs") or [],
        }
        _server_log.info(
            "auto position-reconcile: ghosts %s→%s dupes %s→%s fixed=%s ok=%s",
            ghosts,
            after.get("ghost_count"),
            dupes,
            after.get("duplicate_count"),
            fixed_n,
            result.get("ok"),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        _position_reconcile_last = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ok": False,
            "ghost_count_before": None,
            "ghost_count_after": None,
            "fixed": 0,
            "error": str(exc),
        }
        _server_log.warning("auto position-reconcile failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _position_reconcile_lock.release()


def start_position_reconcile_scheduler() -> None:
    """Every ~30 min close ghost trades that no longer exist on Bybit."""

    interval = max(60.0, float(POSITION_RECONCILE_INTERVAL_SEC))

    def _loop() -> None:
        # First pass shortly after boot (bots may still be starting).
        time.sleep(90)
        while True:
            try:
                run_position_reconcile_auto()
            except Exception as exc:  # noqa: BLE001
                _server_log.warning("position-reconcile scheduler: %s", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name="position-reconcile", daemon=True).start()
    _server_log.info(
        "position-reconcile auto-sync started (every %ss)",
        int(interval),
    )


def effective_strategy_max_open() -> int:
    """
    Total strategy-bot capacity for summary chips: main pool + test pool.

    Pools are separate — do not use max(main, test), which previously made a
    test ceiling of 100 look like the main «Открыто (основные)» limit.
    """
    try:
        strat = max(0, int(load_bot_limits().get("strategy", 0)))
    except Exception:  # noqa: BLE001
        strat = 0
    try:
        test_max = max(0, int(load_test_strategy_settings().get("max_open_trades") or 0))
    except Exception:  # noqa: BLE001
        test_max = 0
    return strat + test_max


def build_open_trades_summary() -> dict[str, Any]:
    """Warm payload for UI «Всего по сделкам» chip (count / margin / PnL)."""
    t0 = time.monotonic()
    try:
        limits = load_bot_limits()
    except Exception:  # noqa: BLE001
        limits = {b: 0 for b in CONFIGS}
    max_total = 0
    for bot in CONFIGS:
        try:
            if bot == "strategy":
                max_total += effective_strategy_max_open()
            else:
                max_total += max(0, int(limits.get(bot, 0)))
        except (TypeError, ValueError):
            pass

    trades: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for bot in CONFIGS:
        # Finder is often offline — keep timeout tiny so warmer stays fast.
        timeout = 0.6 if bot == "finder" else 1.5
        live = _fetch_bot_open_trades_live(bot, timeout=timeout)
        chosen, src = _open_trades_prefer_db_if_live_empty(bot, live)
        sources[bot] = src
        for t in chosen:
            stake = float(t.get("stake_amount") or 0)
            raw_pnl = t.get("total_profit_abs")
            if raw_pnl is None:
                raw_pnl = t.get("profit_abs")
            trades.append(
                {
                    "bot": bot,
                    "trade_id": t.get("trade_id") or t.get("id"),
                    "pair": t.get("pair"),
                    "stake_amount": stake,
                    "profit_abs": float(raw_pnl or 0),
                    "total_profit_abs": float(raw_pnl or 0),
                }
            )

    count = len(trades)
    margin = sum(float(t.get("stake_amount") or 0) for t in trades)
    pnl = sum(float(t.get("total_profit_abs") or t.get("profit_abs") or 0) for t in trades)
    return {
        "count": count,
        "max_total": max_total,
        "margin": round(margin, 4),
        "pnl": round(pnl, 4),
        "sources": sources,
        "built_at": int(time.time() * 1000),
        "build_ms": int((time.monotonic() - t0) * 1000),
    }


# --- Precomputed stats bundle (warm cache for UI) ---
STATS_CACHE_TTL_SEC = 60.0
STATS_CACHE_REFRESH_SEC = 30.0
_stats_cache_lock = threading.RLock()
# key -> {payload, built_at, building, user_id}
_stats_cache: dict[str, dict[str, Any]] = {}


def _stats_tenant_key(user: dict[str, Any] | None = None) -> str:
    u = user if user is not None else (tc.current_user() or {})
    # Demo mirrors admin house trades — share the admin cache key.
    if is_demo_mirror_user(u):
        return "admin"
    if tm.is_admin(u) and not tm.is_impersonating(u):
        return "admin"
    uid = str(u.get("id") or "anon")
    return f"user:{uid}"


def build_stats_bundle(limit: int = 0) -> dict[str, Any]:
    """Assemble closed trades + Bybit history for the current tenant context."""
    with maybe_admin_trade_context() as mirrored:
        t0 = time.monotonic()
        closed = get_closed_trades_payload(limit)
        bybit_history: list[Any] = []
        try:
            ud, env = tc.bybit_context_paths(BASE)
            with tenant_bybit_context(ud, env):
                bybit_history = list(get_history_payload().get("history") or [])
        except Exception as exc:  # noqa: BLE001
            _server_log.warning("stats-bundle bybit history: %s", exc)
        try:
            strategies = get_strategy_catalog()
        except Exception:  # noqa: BLE001
            strategies = [dict(s) for s in AVAILABLE_STRATEGIES]
        open_summary = build_open_trades_summary()
        return {
            "finder": closed.get("finder") or [],
            "strategy": closed.get("strategy") or [],
            "grid": closed.get("grid") or [],
            "bybit_history": bybit_history,
            "strategies": strategies,
            "open_summary": open_summary,
            "built_at": int(time.time() * 1000),
            "build_ms": int((time.monotonic() - t0) * 1000),
            "cached": False,
            "demo_mirror_admin": bool(mirrored),
        }


def _store_stats_bundle(key: str, payload: dict[str, Any], *, user_id: str | None = None) -> None:
    with _stats_cache_lock:
        _stats_cache[key] = {
            "payload": payload,
            "built_at": time.time(),
            "building": False,
            "user_id": user_id,
        }


def _build_and_store_stats(
    key: str,
    user: dict[str, Any] | None,
    limit: int = 0,
) -> dict[str, Any]:
    token = None
    try:
        if user is not None:
            token = tc.set_request_user(user)
        payload = build_stats_bundle(limit)
        uid = str((user or {}).get("id") or "") or None
        _store_stats_bundle(key, payload, user_id=uid)
        return payload
    except Exception:
        with _stats_cache_lock:
            entry = _stats_cache.get(key)
            if entry is not None:
                entry["building"] = False
        raise
    finally:
        if token is not None:
            tc.reset_request_user(token)


def _schedule_stats_rebuild(key: str, user: dict[str, Any] | None) -> None:
    with _stats_cache_lock:
        entry = _stats_cache.setdefault(
            key, {"payload": None, "built_at": 0.0, "building": False, "user_id": None}
        )
        if entry.get("building"):
            return
        entry["building"] = True
    user_snap = dict(user) if user else None

    def _run() -> None:
        try:
            _build_and_store_stats(key, user_snap, 0)
        except Exception as exc:  # noqa: BLE001
            _server_log.warning("stats-bundle rebuild %s: %s", key, exc)
            with _stats_cache_lock:
                entry = _stats_cache.get(key)
                if entry is not None:
                    entry["building"] = False

    threading.Thread(target=_run, name=f"stats-cache-{key}", daemon=True).start()


def get_stats_bundle(*, force: bool = False, max_age: float | None = None) -> dict[str, Any]:
    """Return warm stats payload; rebuild in background when stale."""
    if max_age is None:
        max_age = STATS_CACHE_TTL_SEC
    key = _stats_tenant_key()
    user = tc.current_user()
    now = time.time()

    with _stats_cache_lock:
        entry = _stats_cache.get(key)
        payload = entry.get("payload") if entry else None
        age = now - float(entry.get("built_at") or 0) if entry else 1e9
        fresh = bool(payload) and age <= max_age and not force

    if payload and (fresh or (not force and age < max_age * 3)):
        out = deepcopy(payload)
        out["cached"] = True
        out["age_sec"] = round(age, 2)
        out["stale"] = not fresh
        if force or age > max_age * 0.35:
            _schedule_stats_rebuild(key, user)
        return out

    # Cold start: build once in this request, then keep warm.
    with _stats_cache_lock:
        entry = _stats_cache.setdefault(
            key, {"payload": None, "built_at": 0.0, "building": False, "user_id": None}
        )
        already = bool(entry.get("building"))
        entry["building"] = True
    if already and payload:
        out = deepcopy(payload)
        out["cached"] = True
        out["age_sec"] = round(age, 2)
        out["stale"] = True
        return out
    try:
        built = build_stats_bundle(0)
        uid = str((user or {}).get("id") or "") or None
        _store_stats_bundle(key, built, user_id=uid)
        out = deepcopy(built)
        out["cached"] = False
        return out
    finally:
        with _stats_cache_lock:
            if key in _stats_cache:
                _stats_cache[key]["building"] = False


def start_stats_cache_scheduler() -> None:
    """Keep admin (+ recently used tenant) stats bundles warm."""

    def _loop() -> None:
        time.sleep(3)
        while True:
            try:
                admin_user = {"id": "admin", "role": "admin", "username": "admin"}
                _build_and_store_stats("admin", admin_user, 0)
                with _stats_cache_lock:
                    others = [
                        (k, e.get("user_id"))
                        for k, e in _stats_cache.items()
                        if k != "admin" and e.get("payload") is not None
                    ]
                for key, uid in others:
                    if not uid:
                        continue
                    u = tm.get_user_by_id(str(uid))
                    if u:
                        _build_and_store_stats(key, u, 0)
            except Exception as exc:  # noqa: BLE001
                _server_log.warning("stats cache warmer: %s", exc)
            time.sleep(STATS_CACHE_REFRESH_SEC)

    threading.Thread(target=_loop, name="stats-cache-warmer", daemon=True).start()
    _server_log.info(
        "stats-bundle cache warmer started (ttl=%ss refresh=%ss)",
        int(STATS_CACHE_TTL_SEC),
        int(STATS_CACHE_REFRESH_SEC),
    )


def _normalize_rating_tf(tf: str | None) -> str:
    raw = str(tf or "5m").strip().lower()
    if raw in {"1h", "1hr", "htf", "htf1h"}:
        return "1h"
    return "5m"


def _rating_include_trade(enter_tag: str | None, tf: str, *, bot: str = "strategy") -> bool:
    """5m rating drops 1h strategies. 1h rating keeps only those."""
    is_htf = _strip_enter_tag(enter_tag) in HTF1H_IDS
    if tf == "1h":
        return bot == "strategy" and is_htf
    return not is_htf


def _normalize_rating_bot(bot: str | None) -> str:
    raw = str(bot or "all").strip().lower()
    if raw in {"llm", "deepseek"}:
        return "llm"
    if raw in {"llm50", "deepseek50", "book"}:
        return "llm50"
    if raw in {"cursor"}:
        return "cursor"
    if raw in {"cursor50"}:
        return "cursor50"
    return "all"


def _rating_trade_bot(enter_tag: str | None) -> str:
    base = _strip_enter_tag(enter_tag)
    if base == "Deepseek50" or base.startswith("Deepseek50"):
        return "llm50"
    if base == "DeepseekLlmStrategy" or base.startswith("DeepseekLlmStrategy"):
        return "llm"
    if base == "Cursor50" or base.startswith("Cursor50"):
        return "cursor50"
    if base == "CursorLlmStrategy" or base.startswith("CursorLlmStrategy"):
        return "cursor"
    return ""


def _rating_include_bot(enter_tag: str | None, bot_filter: str) -> bool:
    wanted = _normalize_rating_bot(bot_filter)
    if wanted == "all":
        return True
    return _rating_trade_bot(enter_tag) == wanted


def _normalize_rating_side(side: str | None) -> str:
    raw = str(side or "all").strip().lower()
    if raw in {"long", "buy"}:
        return "long"
    if raw in {"short", "sell"}:
        return "short"
    return "all"


def _rating_include_side(is_short: Any, side: str) -> bool:
    """all keeps every trade; long/short keep one direction. Unknown side is dropped."""
    wanted = _normalize_rating_side(side)
    if wanted == "all":
        return True
    if is_short is None:
        return False
    try:
        flag = bool(int(is_short))
    except (TypeError, ValueError):
        flag = bool(is_short)
    return flag if wanted == "short" else not flag


def _rating_money_and_side(row: tuple[Any, ...], fee_start: int) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Fee fields plus is_short.

    Full SELECT after close_profit_abs: amount, open_rate, close_rate, stake_amount, leverage, is_short.
    Legacy SELECT: is_short alone at fee_start, or absent.
    """
    n = len(row)
    if n >= fee_start + 6:
        return (
            row[fee_start],
            row[fee_start + 1],
            row[fee_start + 2],
            row[fee_start + 3],
            row[fee_start + 4],
            row[fee_start + 5],
        )
    is_short = row[fee_start] if n > fee_start else None
    return None, None, None, None, None, is_short


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


def _enter_tag_is_inv(tag: str | None) -> bool:
    """True when enter_tag marks an inverted (long↔short) trade."""
    return ":inv" in str(tag or "").lower()


def _pair_rating_key(pair: str | None, enter_tag: str | None) -> tuple[str, str, bool]:
    """Unique pair rating id/label; inverted trades get a separate «инв» row."""
    base = str(pair or "").strip() or "—"
    inverted = _enter_tag_is_inv(enter_tag)
    if inverted:
        return f"{base}:inv", f"{base} · инв", True
    return base, base, False


def _strategy_rating_key(
    enter_tag: str | None, labels: dict[str, str] | None = None
) -> tuple[str, str, str, bool]:
    """Unique strategy rating id/label; inverted trades get a separate «инв» row.

    Returns (agg_key, strategy_base, label, inverted).
    """
    base = _strip_enter_tag(enter_tag) or "Без тега"
    inverted = _enter_tag_is_inv(enter_tag)
    nice = (labels or {}).get(base, base)
    if inverted:
        return f"{base}:inv", base, f"{nice} · инв", True
    return base, base, nice, False


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
            # Engine stores naive UTC timestamps
            dt = datetime.strptime(s[:cut], fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp() * 1000.0
        except ValueError:
            continue
    return None


def _rating_period_bounds_ms(period: str) -> tuple[float, float]:
    now = datetime.now(APP_TZ)
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
    for s in [*RETIRED_STRATEGY_LABELS, *AVAILABLE_STRATEGIES]:
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
    out.setdefault("DeepseekLlmStrategy", "DeepSeek")
    out.setdefault("Deepseek50", "DeepSeek 50")
    out.setdefault("CursorLlmStrategy", "Cursor")
    out.setdefault("Cursor50", "Cursor 50")
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
        "_conf_sum": 0.0,
        "_conf_n": 0,
        "_conf_min": None,
        "_conf_max": None,
    }
    if extra:
        row.update(extra)
    return row


def _ml_confidence_pct(raw: Any) -> float | None:
    """Normalize stored ML confidence to percent (0–100)."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    if 0.0 <= val <= 1.0:
        val *= 100.0
    return max(0.0, min(100.0, val))


def _load_trade_confidences(db_path: Path, trade_ids: list[int]) -> dict[int, float]:
    """Best available ml_confidence / ml_gate_confidence per trade id."""
    ids = sorted({int(i) for i in trade_ids if int(i) > 0})
    if not ids or not db_path.is_file():
        return {}
    placeholders = ",".join("?" * len(ids))
    query = f"""
        SELECT ft_trade_id, cd_key, cd_value
        FROM trade_custom_data
        WHERE ft_trade_id IN ({placeholders})
          AND cd_key IN ('ml_confidence', 'ml_gate_confidence')
    """
    raw: dict[int, dict[str, Any]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for tid, key, value in conn.execute(query, ids):
                entry = raw.setdefault(int(tid), {})
                entry[str(key)] = value
    except sqlite3.Error:
        return {}
    out: dict[int, float] = {}
    for tid, keys in raw.items():
        pct = _ml_confidence_pct(keys.get("ml_confidence"))
        if pct is None:
            pct = _ml_confidence_pct(keys.get("ml_gate_confidence"))
        if pct is not None:
            out[tid] = pct
    return out


# Bybit USDT-perp taker fee — same as custom-ui BYBIT_TAKER_FEE_RATE (0.1% open + 0.1% close).
BYBIT_TAKER_FEE_RATE = 0.001


def _pnl_net_of_fees(
    profit_abs: float,
    *,
    amount: float | None = None,
    open_rate: float | None = None,
    close_rate: float | None = None,
    stake_amount: float | None = None,
    leverage: float | None = None,
) -> float:
    """Gross close_profit_abs minus round-trip taker fees (matches history «С комиссией»)."""
    fee = BYBIT_TAKER_FEE_RATE
    fees = 0.0
    try:
        amt = float(amount or 0)
        open_px = float(open_rate or 0)
        close_px = float(close_rate or 0)
    except (TypeError, ValueError):
        amt, open_px, close_px = 0.0, 0.0, 0.0
    if amt > 0 and open_px > 0:
        close_px = close_px if close_px > 0 else open_px
        fees = amt * open_px * fee + amt * close_px * fee
    else:
        try:
            stake = float(stake_amount or 0)
            lev = float(leverage or 1) or 1.0
        except (TypeError, ValueError):
            stake, lev = 0.0, 1.0
        if stake > 0:
            fees = 2.0 * stake * lev * fee
    return float(profit_abs) - fees


def _accumulate_rating(
    row: dict[str, Any], pnl: float, *, confidence_pct: float | None = None
) -> None:
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
    if confidence_pct is not None:
        row["_conf_sum"] = float(row.get("_conf_sum") or 0.0) + float(confidence_pct)
        row["_conf_n"] = int(row.get("_conf_n") or 0) + 1
        cur_min = row.get("_conf_min")
        cur_max = row.get("_conf_max")
        row["_conf_min"] = (
            float(confidence_pct)
            if cur_min is None
            else min(float(cur_min), float(confidence_pct))
        )
        row["_conf_max"] = (
            float(confidence_pct)
            if cur_max is None
            else max(float(cur_max), float(confidence_pct))
        )


def _finalize_rating_row(row: dict[str, Any]) -> dict[str, Any]:
    row["pnl"] = round(float(row["pnl"]), 4)
    row["income"] = round(float(row["income"]), 4)
    row["loss_sum"] = round(float(row["loss_sum"]), 4)
    row["loss_abs"] = round(abs(float(row["loss_sum"])), 4)
    n = int(row["trades"]) or 1
    row["winrate"] = round(100.0 * int(row["wins"]) / n, 1)
    row["avg_pnl"] = round(float(row["pnl"]) / n, 4)
    conf_n = int(row.pop("_conf_n", 0) or 0)
    conf_sum = float(row.pop("_conf_sum", 0.0) or 0.0)
    conf_min = row.pop("_conf_min", None)
    conf_max = row.pop("_conf_max", None)
    if conf_n > 0:
        row["conf_count"] = conf_n
        row["conf_avg"] = round(conf_sum / conf_n, 1)
        row["conf_min"] = round(float(conf_min), 1) if conf_min is not None else None
        row["conf_max"] = round(float(conf_max), 1) if conf_max is not None else None
    else:
        row["conf_count"] = 0
        row["conf_avg"] = None
        row["conf_min"] = None
        row["conf_max"] = None
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
    period: str = "all",
    limit: int = 40,
    user_id: str | None = None,
    tf: str = "5m",
    bot: str = "all",
    side: str = "all",
) -> dict[str, Any]:
    """Top strategies by net PnL across all users' strategy bots (or one user)."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 40), 100))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    tf = _normalize_rating_tf(tf)
    bot_filter = _normalize_rating_bot(bot)
    side_filter = _normalize_rating_side(side)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    agg: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT id, enter_tag, close_date, close_profit_abs,
               amount, open_rate, close_rate, stake_amount, leverage, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT id, enter_tag, close_date, close_profit_abs, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, db_path in iter_all_strategy_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(query_legacy).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("strategy rating %s %s: %s", uid, db_path, exc)
            sources.append({"user": uid, "db": str(db_path), "trades": 0, "error": str(exc)})
            continue
        period_rows: list[tuple[Any, ...]] = []
        trade_ids: list[int] = []
        for row_t in rows:
            trade_id = row_t[0]
            enter_tag = row_t[1]
            close_date = row_t[2]
            profit_abs = row_t[3]
            amount, open_rate, close_rate, stake_amount, leverage, is_short = _rating_money_and_side(
                row_t, 4
            )
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            if not _rating_include_trade(enter_tag, tf):
                continue
            if not _rating_include_bot(enter_tag, bot_filter):
                continue
            if not _rating_include_side(is_short, side_filter):
                continue
            period_rows.append(
                (trade_id, enter_tag, profit_abs, amount, open_rate, close_rate, stake_amount, leverage)
            )
            try:
                trade_ids.append(int(trade_id))
            except (TypeError, ValueError):
                pass
        conf_map = _load_trade_confidences(Path(db_path), trade_ids)
        for (
            trade_id,
            enter_tag,
            profit_abs,
            amount,
            open_rate,
            close_rate,
            stake_amount,
            leverage,
        ) in period_rows:
            sid, sid_base, sid_label, inverted = _strategy_rating_key(enter_tag, labels)
            try:
                gross = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            pnl = _pnl_net_of_fees(
                gross,
                amount=amount,
                open_rate=open_rate,
                close_rate=close_rate,
                stake_amount=stake_amount,
                leverage=leverage,
            )
            row = agg.setdefault(
                sid,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_base": sid_base,
                        "label": sid_label,
                        "inverted": inverted,
                    }
                ),
            )
            try:
                tid = int(trade_id)
            except (TypeError, ValueError):
                tid = 0
            _accumulate_rating(row, pnl, confidence_pct=conf_map.get(tid))
            used += 1
        sources.append({"user": uid, "db": str(db_path.name), "trades": used})

    rows = [_finalize_rating_row(r) for r in agg.values()]
    ranked = _rank_rating_rows(rows, limit=limit, key="pnl")
    return {
        "period": period,
        "tf": tf,
        "side": side_filter,
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


_STRATEGY_TOP_PAIRS = 12


def _pair_coin_label(pair: str | None) -> str:
    raw = str(pair or "").strip()
    if not raw:
        return "—"
    base = raw.split("/")[0].replace(":USDT", "")
    return base or raw


def get_strategy_pair_rating_payload(
    period: str = "all",
    limit: int = 100,
    user_id: str | None = None,
    tf: str = "5m",
    bot: str = "all",
    side: str = "all",
) -> dict[str, Any]:
    """Strategies ranked by net PnL; each row expands to its top pairs."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 100), 100))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    tf = _normalize_rating_tf(tf)
    bot_filter = _normalize_rating_bot(bot)
    side_filter = _normalize_rating_side(side)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    agg: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT id, pair, enter_tag, close_date, close_profit_abs,
               amount, open_rate, close_rate, stake_amount, leverage, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT id, pair, enter_tag, close_date, close_profit_abs, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, db_path in iter_all_strategy_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(query_legacy).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("strategy-pair rating %s %s: %s", uid, db_path, exc)
            sources.append({"user": uid, "db": str(db_path), "trades": 0, "error": str(exc)})
            continue
        period_rows: list[tuple[Any, ...]] = []
        trade_ids: list[int] = []
        for row_t in rows:
            trade_id = row_t[0]
            pair = row_t[1]
            enter_tag = row_t[2]
            close_date = row_t[3]
            profit_abs = row_t[4]
            amount, open_rate, close_rate, stake_amount, leverage, is_short = _rating_money_and_side(
                row_t, 5
            )
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            if not _rating_include_trade(enter_tag, tf):
                continue
            if not _rating_include_bot(enter_tag, bot_filter):
                continue
            if not _rating_include_side(is_short, side_filter):
                continue
            period_rows.append(
                (
                    trade_id,
                    pair,
                    enter_tag,
                    profit_abs,
                    amount,
                    open_rate,
                    close_rate,
                    stake_amount,
                    leverage,
                )
            )
            try:
                trade_ids.append(int(trade_id))
            except (TypeError, ValueError):
                pass
        conf_map = _load_trade_confidences(Path(db_path), trade_ids)
        for (
            trade_id,
            pair,
            enter_tag,
            profit_abs,
            amount,
            open_rate,
            close_rate,
            stake_amount,
            leverage,
        ) in period_rows:
            sid, sid_base, sid_label, inverted = _strategy_rating_key(enter_tag, labels)
            pair_full = str(pair or "").strip() or "—"
            pair_label = _pair_coin_label(pair_full)
            try:
                gross = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            pnl = _pnl_net_of_fees(
                gross,
                amount=amount,
                open_rate=open_rate,
                close_rate=close_rate,
                stake_amount=stake_amount,
                leverage=leverage,
            )
            row = agg.setdefault(
                sid,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_base": sid_base,
                        "label": sid_label,
                        "inverted": inverted,
                        "_details": {},
                    }
                ),
            )
            try:
                tid = int(trade_id)
            except (TypeError, ValueError):
                tid = 0
            conf = conf_map.get(tid)
            _accumulate_rating(row, pnl, confidence_pct=conf)
            dmap: dict[str, Any] = row["_details"]
            drow = dmap.setdefault(
                pair_full,
                _new_rating_bucket(
                    {
                        "pair": pair_full,
                        "pair_base": pair_label,
                        "label": pair_label,
                        "strategy_label": pair_label,
                    }
                ),
            )
            _accumulate_rating(drow, pnl, confidence_pct=conf)
            used += 1
        sources.append({"user": uid, "db": str(db_path.name), "trades": used})

    rows: list[dict[str, Any]] = []
    for srow in agg.values():
        details_raw = list((srow.pop("_details", {}) or {}).values())
        details = [_finalize_rating_row(d) for d in details_raw]
        details.sort(key=lambda x: float(x.get("pnl") or 0.0), reverse=True)
        row = _finalize_rating_row(srow)
        row["pairs_total"] = len(details)
        row["details"] = details[:_STRATEGY_TOP_PAIRS]
        row["details_count"] = len(row["details"])
        rows.append(row)

    ranked = _rank_rating_rows(rows, limit=limit, key="pnl")
    return {
        "period": period,
        "tf": tf,
        "side": side_filter,
        "limit": limit,
        "mode": "strategy_pairs",
        "top_pairs": _STRATEGY_TOP_PAIRS,
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len({s.get("user") for s in sources}),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "strategies_total": len(rows),
        "rows": ranked,
    }


def get_pair_rating_payload(
    period: str = "all",
    limit: int = 100,
    user_id: str | None = None,
    tf: str = "5m",
    bot: str = "all",
    side: str = "all",
) -> dict[str, Any]:
    """Pair ranking across all users/bots (or one user), with strategy+bot detail."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 100), 200))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    tf = _normalize_rating_tf(tf)
    bot_filter = _normalize_rating_bot(bot)
    side_filter = _normalize_rating_side(side)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    # pair -> aggregates + details keyed by (bot, strategy_id)
    agg: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT id, pair, enter_tag, strategy, close_date, close_profit_abs,
               amount, open_rate, close_rate, stake_amount, leverage, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT id, pair, enter_tag, strategy, close_date, close_profit_abs, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, bot, db_path in iter_all_trade_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        if tf == "1h" and bot != "strategy":
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(query_legacy).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("pair rating %s/%s %s: %s", uid, bot, db_path, exc)
            sources.append(
                {"user": uid, "bot": bot, "db": str(db_path), "trades": 0, "error": str(exc)}
            )
            continue
        bot_label = _BOT_RATING_LABELS.get(bot, bot)
        period_rows: list[tuple[Any, ...]] = []
        trade_ids: list[int] = []
        for row_t in rows:
            trade_id = row_t[0]
            pair = row_t[1]
            enter_tag = row_t[2]
            strategy_name = row_t[3]
            close_date = row_t[4]
            profit_abs = row_t[5]
            amount, open_rate, close_rate, stake_amount, leverage, is_short = _rating_money_and_side(
                row_t, 6
            )
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            if not _rating_include_trade(enter_tag, tf, bot=bot):
                continue
            if not _rating_include_bot(enter_tag, bot_filter):
                continue
            if not _rating_include_side(is_short, side_filter):
                continue
            period_rows.append(
                (
                    trade_id,
                    pair,
                    enter_tag,
                    strategy_name,
                    profit_abs,
                    amount,
                    open_rate,
                    close_rate,
                    stake_amount,
                    leverage,
                )
            )
            try:
                trade_ids.append(int(trade_id))
            except (TypeError, ValueError):
                pass
        conf_map = _load_trade_confidences(Path(db_path), trade_ids)
        for (
            trade_id,
            pair,
            enter_tag,
            strategy_name,
            profit_abs,
            amount,
            open_rate,
            close_rate,
            stake_amount,
            leverage,
        ) in period_rows:
            pair_id, pair_label, inverted = _pair_rating_key(pair, enter_tag)
            pair_base = str(pair or "").strip() or "—"
            sid = _strip_enter_tag(enter_tag)
            if not sid:
                sid = str(strategy_name or "").strip() or bot_label
            try:
                gross = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            pnl = _pnl_net_of_fees(
                gross,
                amount=amount,
                open_rate=open_rate,
                close_rate=close_rate,
                stake_amount=stake_amount,
                leverage=leverage,
            )
            try:
                tid = int(trade_id)
            except (TypeError, ValueError):
                tid = 0
            conf = conf_map.get(tid)

            prow = agg.setdefault(
                pair_id,
                _new_rating_bucket(
                    {
                        "pair": pair_id,
                        "pair_base": pair_base,
                        "label": pair_label,
                        "inverted": inverted,
                        "_details": {},
                    }
                ),
            )
            _accumulate_rating(prow, pnl, confidence_pct=conf)

            detail_key = f"{bot}|{sid}"
            dmap: dict[str, Any] = prow["_details"]
            strat_label = labels.get(sid, sid)
            if inverted and not str(strat_label).endswith("инв"):
                detail_label = f"{strat_label} · инв · {bot_label}"
            else:
                detail_label = f"{strat_label} · {bot_label}"
            drow = dmap.setdefault(
                detail_key,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_label": strat_label,
                        "bot": bot,
                        "bot_label": bot_label,
                        "label": detail_label,
                        "inverted": inverted,
                    }
                ),
            )
            _accumulate_rating(drow, pnl, confidence_pct=conf)
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
        "tf": tf,
        "side": side_filter,
        "limit": limit,
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len(users),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "pairs_total": len(rows),
        "rows": ranked,
    }


# Monday-first weekday keys (Python datetime.weekday()).
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS_RU = {
    "mon": "Понедельник",
    "tue": "Вторник",
    "wed": "Среда",
    "thu": "Четверг",
    "fri": "Пятница",
    "sat": "Суббота",
    "sun": "Воскресенье",
}
_WEEKDAY_SHORT_RU = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}


def _close_weekday_key(close_date: Any) -> str | None:
    """Weekday key in APP_TZ: mon…sun."""
    ms = _parse_close_date_ms(close_date)
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=APP_TZ)
    except (OSError, OverflowError, ValueError):
        return None
    idx = int(dt.weekday())  # Mon=0 … Sun=6
    if idx < 0 or idx >= len(_WEEKDAY_KEYS):
        return None
    return _WEEKDAY_KEYS[idx]


def _weekday_label(day_key: str) -> str:
    return _WEEKDAY_LABELS_RU.get(str(day_key or ""), str(day_key or "—"))


def _weekday_order(day_key: str) -> int:
    try:
        return _WEEKDAY_KEYS.index(str(day_key or ""))
    except ValueError:
        return 99


def get_strategy_daily_rating_payload(
    period: str = "all",
    limit: int = 7,
    user_id: str | None = None,
    tf: str = "5m",
    bot: str = "all",
    side: str = "all",
) -> dict[str, Any]:
    """Strategy PnL by weekday (Mon–Sun); expand day → strategies."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    # Always at most 7 weekdays; limit kept for API compatibility.
    limit = max(1, min(int(limit or 7), 7))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    tf = _normalize_rating_tf(tf)
    bot_filter = _normalize_rating_bot(bot)
    side_filter = _normalize_rating_side(side)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    # Pre-create Mon…Sun buckets so empty weekdays still appear.
    agg: dict[str, dict[str, Any]] = {
        key: _new_rating_bucket(
            {
                "day": key,
                "weekday": key,
                "weekday_order": i,
                "label": _weekday_label(key),
                "_details": {},
            }
        )
        for i, key in enumerate(_WEEKDAY_KEYS)
    }
    sources: list[dict[str, Any]] = []
    query = """
        SELECT id, enter_tag, close_date, close_profit_abs,
               amount, open_rate, close_rate, stake_amount, leverage, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT id, enter_tag, close_date, close_profit_abs, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, db_path in iter_all_strategy_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(query_legacy).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("weekday rating %s %s: %s", uid, db_path, exc)
            sources.append({"user": uid, "db": str(db_path), "trades": 0, "error": str(exc)})
            continue

        period_rows: list[tuple[Any, ...]] = []
        trade_ids: list[int] = []
        for row_t in rows:
            trade_id = row_t[0]
            enter_tag = row_t[1]
            close_date = row_t[2]
            profit_abs = row_t[3]
            amount, open_rate, close_rate, stake_amount, leverage, is_short = _rating_money_and_side(
                row_t, 4
            )
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            if not _rating_include_trade(enter_tag, tf):
                continue
            if not _rating_include_bot(enter_tag, bot_filter):
                continue
            if not _rating_include_side(is_short, side_filter):
                continue
            day = _close_weekday_key(close_date)
            if not day:
                continue
            period_rows.append(
                (
                    trade_id,
                    enter_tag,
                    day,
                    profit_abs,
                    amount,
                    open_rate,
                    close_rate,
                    stake_amount,
                    leverage,
                )
            )
            try:
                trade_ids.append(int(trade_id))
            except (TypeError, ValueError):
                pass

        conf_map = _load_trade_confidences(Path(db_path), trade_ids)
        for (
            trade_id,
            enter_tag,
            day,
            profit_abs,
            amount,
            open_rate,
            close_rate,
            stake_amount,
            leverage,
        ) in period_rows:
            sid, sid_base, sid_label, inverted = _strategy_rating_key(enter_tag, labels)
            try:
                gross = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            pnl = _pnl_net_of_fees(
                gross,
                amount=amount,
                open_rate=open_rate,
                close_rate=close_rate,
                stake_amount=stake_amount,
                leverage=leverage,
            )
            try:
                tid = int(trade_id)
            except (TypeError, ValueError):
                tid = 0
            conf = conf_map.get(tid)

            drow = agg[day]
            _accumulate_rating(drow, pnl, confidence_pct=conf)

            details_map: dict[str, Any] = drow["_details"]
            srow = details_map.setdefault(
                sid,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_base": sid_base,
                        "strategy_label": sid_label,
                        "label": sid_label,
                        "inverted": inverted,
                        "day": day,
                        "weekday": day,
                        "weekday_order": _weekday_order(day),
                    }
                ),
            )
            _accumulate_rating(srow, pnl, confidence_pct=conf)
            used += 1
        sources.append({"user": uid, "db": str(db_path.name), "trades": used})

    rows: list[dict[str, Any]] = []
    for key in _WEEKDAY_KEYS:
        drow = agg[key]
        details_raw = list((drow.pop("_details", {}) or {}).values())
        details = [_finalize_rating_row(d) for d in details_raw]
        details.sort(key=lambda x: float(x.get("pnl") or 0.0), reverse=True)
        row = _finalize_rating_row(drow)
        row["day"] = key
        row["weekday"] = key
        row["weekday_order"] = _weekday_order(key)
        row["label"] = _weekday_label(key)
        row["details"] = details
        row["details_count"] = len(details)
        row["strategies_count"] = len(details)
        rows.append(row)

    # Default order: Monday → Sunday (client can re-sort by PnL etc.).
    rows.sort(key=lambda x: int(x.get("weekday_order") or 99))
    rows = rows[:limit]
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    return {
        "period": period,
        "tf": tf,
        "side": side_filter,
        "limit": limit,
        "mode": "weekday",
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len({s.get("user") for s in sources}),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "days_total": 7,
        "rows": rows,
    }


def get_strategy_weekday_matrix_payload(
    period: str = "all",
    limit: int = 100,
    user_id: str | None = None,
    tf: str = "5m",
    bot: str = "all",
    side: str = "all",
) -> dict[str, Any]:
    """Matrix: strategies as rows, weekdays (Mon–Sun) as columns, cell = net PnL."""
    period = str(period or "all").strip().lower()
    if period not in {"today", "yesterday", "7d", "30d", "all"}:
        period = "all"
    limit = max(1, min(int(limit or 100), 200))
    user_filter, user_label = _resolve_rating_user_filter(user_id)
    tf = _normalize_rating_tf(tf)
    bot_filter = _normalize_rating_bot(bot)
    side_filter = _normalize_rating_side(side)
    from_ms, to_ms = _rating_period_bounds_ms(period)
    labels = _strategy_label_map()

    # strategy_id -> total bucket + per-weekday buckets
    totals: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, dict[str, Any]]] = {}
    sources: list[dict[str, Any]] = []
    query = """
        SELECT id, enter_tag, close_date, close_profit_abs,
               amount, open_rate, close_rate, stake_amount, leverage, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    query_legacy = """
        SELECT id, enter_tag, close_date, close_profit_abs, is_short
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """

    for uid, db_path in iter_all_strategy_db_paths():
        if user_filter is not None and uid != user_filter:
            continue
        used = 0
        try:
            with sqlite3.connect(db_path) as conn:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(query_legacy).fetchall()
        except sqlite3.Error as exc:
            _server_log.warning("weekday matrix %s %s: %s", uid, db_path, exc)
            sources.append({"user": uid, "db": str(db_path), "trades": 0, "error": str(exc)})
            continue

        period_rows: list[tuple[Any, ...]] = []
        for row_t in rows:
            trade_id = row_t[0]
            enter_tag = row_t[1]
            close_date = row_t[2]
            profit_abs = row_t[3]
            amount, open_rate, close_rate, stake_amount, leverage, is_short = _rating_money_and_side(
                row_t, 4
            )
            close_ms = _parse_close_date_ms(close_date)
            if close_ms is None or close_ms < from_ms or close_ms > to_ms:
                continue
            if not _rating_include_trade(enter_tag, tf):
                continue
            if not _rating_include_bot(enter_tag, bot_filter):
                continue
            if not _rating_include_side(is_short, side_filter):
                continue
            day = _close_weekday_key(close_date)
            if not day:
                continue
            period_rows.append(
                (enter_tag, day, profit_abs, amount, open_rate, close_rate, stake_amount, leverage)
            )

        for enter_tag, day, profit_abs, amount, open_rate, close_rate, stake_amount, leverage in period_rows:
            sid, sid_base, sid_label, inverted = _strategy_rating_key(enter_tag, labels)
            try:
                gross = float(profit_abs or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            pnl = _pnl_net_of_fees(
                gross,
                amount=amount,
                open_rate=open_rate,
                close_rate=close_rate,
                stake_amount=stake_amount,
                leverage=leverage,
            )
            total = totals.setdefault(
                sid,
                _new_rating_bucket(
                    {
                        "strategy_id": sid,
                        "strategy_base": sid_base,
                        "label": sid_label,
                        "inverted": inverted,
                    }
                ),
            )
            _accumulate_rating(total, pnl)
            day_map = by_day.setdefault(sid, {})
            cell = day_map.setdefault(day, _new_rating_bucket({"day": day, "weekday": day}))
            _accumulate_rating(cell, pnl)
            used += 1
        sources.append({"user": uid, "db": str(db_path.name), "trades": used})

    weekdays = [
        {
            "key": k,
            "label": _WEEKDAY_SHORT_RU[k],
            "label_full": _WEEKDAY_LABELS_RU[k],
            "order": i,
        }
        for i, k in enumerate(_WEEKDAY_KEYS)
    ]

    rows: list[dict[str, Any]] = []
    for sid, total in totals.items():
        finalized = _finalize_rating_row(total)
        cells: dict[str, Any] = {}
        for key in _WEEKDAY_KEYS:
            raw = (by_day.get(sid) or {}).get(key)
            if raw is None:
                cells[key] = {
                    "day": key,
                    "pnl": 0.0,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "income": 0.0,
                    "loss_abs": 0.0,
                    "winrate": None,
                }
            else:
                cell = _finalize_rating_row(dict(raw))
                cells[key] = {
                    "day": key,
                    "pnl": cell["pnl"],
                    "trades": cell["trades"],
                    "wins": cell["wins"],
                    "losses": cell["losses"],
                    "income": cell["income"],
                    "loss_abs": cell["loss_abs"],
                    "winrate": cell["winrate"],
                }
            # Flat keys for client sort: pnl_mon, trades_tue, …
            finalized[f"pnl_{key}"] = float(cells[key]["pnl"])
            finalized[f"trades_{key}"] = int(cells[key]["trades"])
        finalized["by_weekday"] = cells
        rows.append(finalized)

    ranked = _rank_rating_rows(rows, limit=limit, key="pnl")
    return {
        "period": period,
        "tf": tf,
        "side": side_filter,
        "limit": limit,
        "mode": "weekday_matrix",
        "weekdays": weekdays,
        "user_filter": user_filter,
        "user_label": user_label,
        "sources": sources,
        "users": len({s.get("user") for s in sources}),
        "trades_total": sum(int(s.get("trades") or 0) for s in sources),
        "strategies_total": len(rows),
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
    """Read strategy bot SL/TP from SQLite user_settings.bot_limits.

    Config JSON is only a fallback. bot_limits.json is a mirror and must not
    override the DB: a later stake/max-trades save rewrites that file from SQLite.
    """
    cfg_path = CONFIGS.get("strategy")
    if cfg_path and cfg_path.is_file():
        risk = _strategy_risk_from_config(load_config(cfg_path))
    else:
        risk = {
            "stoploss": DEFAULT_STRATEGY_STOPLOSS,
            "take_profit": DEFAULT_STRATEGY_TAKE_PROFIT,
        }
    try:
        uid = user_prefs.settings_owner_id()
        data = user_prefs.load_bot_limits_payload(uid, base=BASE)
    except Exception:
        data = {}
    stored = data.get("strategy_risk") if isinstance(data, dict) else None
    if isinstance(stored, dict):
        if "stoploss" in stored:
            try:
                risk["stoploss"] = float(stored["stoploss"])
            except (TypeError, ValueError):
                pass
        if "take_profit" in stored:
            try:
                risk["take_profit"] = float(stored["take_profit"])
            except (TypeError, ValueError):
                pass
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
    """Persist global SL/TP into SQLite (source of truth) and the JSON mirror."""
    uid = user_prefs.settings_owner_id()
    payload = load_bot_limits_file()
    payload["strategy_risk"] = {
        "stoploss": float(risk["stoploss"]),
        "take_profit": float(risk["take_profit"]),
    }
    user_prefs.save_bot_limits_payload(uid, payload, base=BASE)


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


def _explicit_sltp_ids() -> set[str]:
    uid = user_prefs.settings_owner_id()
    payload = user_prefs.load_enabled_payload(uid, base=BASE)
    stored = _object_map(payload.get("sltp") if isinstance(payload, dict) else None)
    out: set[str] = set()
    for sid, raw in stored.items():
        if isinstance(raw, dict) and (raw.get("sl") is not None or raw.get("tp") is not None):
            out.add(str(sid))
    return out


def _clear_trained_risk_for_panel(panel: str) -> None:
    """Turn off «SL/TP из обучения» so a just-saved block SL/TP is what trades use.

    A strategy that already has its own stop/take keeps that choice. The main
    save does not touch 1h strategies: they use the per-strategy menu.
    """
    enabled = load_enabled_map()
    trained = load_trained_risk_map()
    explicit = _explicit_sltp_ids()
    placement = ensure_strategy_ui_placement()
    changed = False
    for sid, on in list(trained.items()):
        if not on or sid in explicit:
            continue
        if panel == "main" and sid in HTF1H_IDS:
            continue
        if effective_ui_panel(sid, placement) != panel:
            continue
        trained[sid] = False
        changed = True
    if changed:
        save_enabled_map(enabled, trained_risk=trained)


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
    # SQLite is what the executor reads. A missing tenant config must not
    # abort the save (that dropped pasha's general SL/TP on the floor).
    save_strategy_risk_to_limits(risk)
    config_warning: str | None = None
    try:
        apply_strategy_risk_to_config(risk)
    except Exception as exc:  # noqa: BLE001
        config_warning = str(exc)
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
    if config_warning and not reload_warning:
        reload_warning = config_warning
    if reload_warning:
        result["reload_warning"] = reload_warning
    if old != risk:
        try:
            record_strategy_risk(old, risk)
        except Exception:
            pass
    return result


def load_bot_limits_file() -> dict[str, Any]:
    """Read persistent bot limits (max trades + stakes) from per-user DB."""
    uid = user_prefs.settings_owner_id()
    data = user_prefs.load_bot_limits_payload(uid, base=BASE)
    payload = _default_bot_limits_payload()
    if not data:
        # Seed from configs once into DB
        if bot_limits_file().is_file():
            try:
                data = json.loads(bot_limits_file().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if data:
            user_prefs.save_bot_limits_payload(uid, data, base=BASE)
        else:
            # Build from live configs
            snap = _default_bot_limits_payload()
            for bot, path in CONFIGS.items():
                try:
                    cfg = load_config(path)
                    raw = cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2))
                    if raw == float("inf"):
                        raw = INF_MAX_OPEN_TRADES
                    snap["max_open_trades"][bot] = int(raw)
                    snap["stake_amount"][bot] = float(
                        cfg.get("stake_amount", DEFAULT_STAKES.get(bot, 5))
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            user_prefs.save_bot_limits_payload(uid, snap, base=BASE)
            data = snap
    stored_trades = data.get("max_open_trades", data if "stake_amount" not in data else {})
    for bot in CONFIGS:
        if bot in stored_trades:
            try:
                payload["max_open_trades"][bot] = int(stored_trades[bot])
            except (TypeError, ValueError):
                pass
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
    if data.get("htf_stake_amount") is not None:
        try:
            payload["htf_stake_amount"] = float(data["htf_stake_amount"])
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


def load_htf_stake() -> float:
    """Per-user stake for the 1h panel. Falls back to the strategy stake."""
    payload = load_bot_limits_file()
    raw = payload.get("htf_stake_amount")
    if raw is None:
        return float(payload["stake_amount"].get("strategy", DEFAULT_STAKES.get("strategy", 5)))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(payload["stake_amount"].get("strategy", DEFAULT_STAKES.get("strategy", 5)))


def set_htf_stake(value: float) -> dict[str, Any]:
    """Stake used only for 1-hour strategy entries of the current user."""
    value = float(value)
    if value < MIN_STAKE_AMOUNT or value > MAX_STAKE_AMOUNT:
        raise ValueError(
            f"stake_amount must be between {MIN_STAKE_AMOUNT} and {MAX_STAKE_AMOUNT}"
        )
    uid = user_prefs.settings_owner_id()
    payload = load_bot_limits_file()
    payload["htf_stake_amount"] = value
    user_prefs.save_bot_limits_payload(uid, payload, base=BASE)
    return {
        "htf_stake_amount": value,
        "limits": bot_limits_public_payload(),
        **get_strategies_payload(),
    }


def snapshot_limits_from_configs() -> dict[str, int]:
    """Legacy CLI: NEVER overwrite DB from configs when limits already exist.

    Config files are write-through mirrors; SQLite user_settings.bot_limits is
    the source of truth. Only seed once if DB/payload is empty.
    """
    uid = user_prefs.settings_owner_id()
    existing = user_prefs.load_bot_limits_payload(uid, base=BASE)
    stored = existing.get("max_open_trades") if isinstance(existing, dict) else None
    if isinstance(stored, dict) and any(bot in stored for bot in CONFIGS):
        return apply_bot_limits_to_configs()
    limits: dict[str, int] = {}
    stakes: dict[str, float] = dict(DEFAULT_STAKES)
    for bot, path in CONFIGS.items():
        try:
            cfg = load_config(path)
            raw = cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2))
            if raw == float("inf"):
                raw = INF_MAX_OPEN_TRADES
            limits[bot] = int(raw)
            stakes[bot] = float(cfg.get("stake_amount", DEFAULT_STAKES.get(bot, 5)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            limits[bot] = DEFAULT_BOT_LIMITS.get(bot, 2)
    save_bot_limits(limits, stakes=stakes)
    return apply_bot_limits_to_configs()


def ensure_bot_limits_snapshot() -> dict[str, int]:
    """Ensure DB limits exist (migrate/seed) and mirror into config files."""
    load_bot_limits_file()
    return apply_bot_limits_to_configs()


def bot_limits_public_payload() -> dict[str, Any]:
    """Canonical limits for UI/API — always from SQLite user_settings."""
    payload = load_bot_limits_file()
    return {
        "max_open_trades": {
            bot: int(payload["max_open_trades"][bot]) for bot in CONFIGS
        },
        "stake_amount": {
            bot: float(payload["stake_amount"].get(bot, DEFAULT_STAKES.get(bot, 5)))
            for bot in DEFAULT_STAKES
        },
        "max_open_trades_per_strategy": int(
            payload.get("max_open_trades_per_strategy", DEFAULT_MAX_OPEN_TRADES_PER_STRATEGY)
        ),
        "strategy_risk": dict(payload.get("strategy_risk") or {}),
        "htf_stake_amount": load_htf_stake(),
        "source": "user_settings.bot_limits",
    }


def _soft_reload_bot_once(bot: str) -> tuple[int, str | None]:
    """One best-effort reload_config. No retries/background loops."""
    if bot == "strategy" and _shared_strategy_uses_signal_engine():
        _ensure_shared_signal_engine_max_open_one()
        return 0, None
    try:
        api_call(
            RELOAD[bot],
            "POST",
            retries=1,
            timeout=min(LIMIT_RELOAD_TIMEOUT, 8.0),
        )
        return 1, None
    except BaseException as exc:  # noqa: BLE001
        return 0, str(exc)


def save_bot_limits(
    limits: dict[str, int],
    *,
    grid_stake: float | None = None,
    stakes: dict[str, float] | None = None,
    max_open_trades_per_strategy: int | None = None,
) -> None:
    uid = user_prefs.settings_owner_id()
    payload = load_bot_limits_file()
    payload["max_open_trades"] = {bot: int(limits[bot]) for bot in CONFIGS if bot in limits}
    if stakes:
        for bot, value in stakes.items():
            if bot in DEFAULT_STAKES:
                payload["stake_amount"][bot] = float(value)
    if grid_stake is not None:
        payload["stake_amount"]["grid"] = float(grid_stake)
    if max_open_trades_per_strategy is not None:
        payload["max_open_trades_per_strategy"] = int(max_open_trades_per_strategy)
    user_prefs.save_bot_limits_payload(uid, payload, base=BASE)


def _shared_strategy_uses_signal_engine() -> bool:
    """Admin shared stack: strategy UI/API points at signal-engine (:8081).

    CONFIGS['strategy'] is still config_strategy.json (disk mirror for limits),
    but live show_config/reload hit config_signal_engine.json which must stay
    at max_open_trades=1 (no local fills). Real slot cap is bot_limits →
    trade-executor.
    """
    try:
        strat_path = CONFIGS.get("strategy")
    except Exception:
        return False
    if not strat_path or Path(strat_path).name != "config_strategy.json":
        return False
    se = BASE / "user_data" / "config_signal_engine.json"
    return se.is_file()


def _ensure_shared_signal_engine_max_open_one() -> bool:
    """Keep shared signal-engine at max_open_trades=1 (signal-only, no fills)."""
    if not _shared_strategy_uses_signal_engine():
        return False
    path = BASE / "user_data" / "config_signal_engine.json"
    cfg = load_config(path)
    if int(cfg.get("max_open_trades") or 0) == 1:
        return False
    cfg["max_open_trades"] = 1
    save_config(path, cfg)
    return True


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
    # Never push strategy bot_limits into signal-engine — it must stay at 1.
    _ensure_shared_signal_engine_max_open_one()
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


def apply_tenant_bot_limits() -> None:
    """Apply each tenant's bot_limits.json into that tenant's config files."""
    try:
        rows = tm.load_users()
    except Exception:  # noqa: BLE001
        return
    for user in rows or []:
        if not isinstance(user, dict) or tm.is_admin(user):
            continue
        if not user.get("enabled", True):
            continue
        token = tc.set_request_user(user)
        try:
            apply_bot_limits_to_configs()
        except Exception as exc:  # noqa: BLE001
            _server_log.warning("tenant bot_limits %s: %s", user.get("id"), exc)
        finally:
            tc.reset_request_user(token)

RELOAD_RETRIES = 8
RELOAD_RETRY_DELAY = 2.0
# Faster path for limit/stake apply (full RELOAD_RETRIES×30s blocks the UI).
LIMIT_RELOAD_TIMEOUT = 12.0
LIMIT_RELOAD_ATTEMPTS = 3


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
    timeout: float = 30.0,
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _live_max_open_trades(bot: str, *, timeout: float = 4.0) -> int | None:
    """Read max_open_trades from the running bot (None if unreachable)."""
    bot = resolve_bot(bot)
    try:
        url = _bot_api_v1_url(bot, "show_config")
        req = urllib.request.Request(url, headers={"Authorization": _basic_header()}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"null") or {}
        raw = data.get("max_open_trades")
        if raw == float("inf"):
            return INF_MAX_OPEN_TRADES
        return int(raw)
    except (TypeError, ValueError, OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def ensure_live_max_open_trades(bot: str, expected: int) -> dict[str, Any]:
    """Deprecated no-op: DB bot_limits is authoritative; do not poll/reload-loop."""
    bot = resolve_bot(bot)
    expected = int(expected)
    if bot == "strategy" and _shared_strategy_uses_signal_engine():
        _ensure_shared_signal_engine_max_open_one()
    return {
        "live_applied": True,
        "live_max_open_trades": expected,
        "reloads": 0,
        "via": "user_settings.bot_limits",
    }


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
    """Legacy file flag — ignored for Start/Stop (control_plane SQLite is source of truth)."""
    return True


def reload_bot(bot: str, *, notify: bool = False, user_id: str = "admin") -> Any:
    result = api_call(
        RELOAD[bot],
        "POST",
        retries=RELOAD_RETRIES,
        retry_delay=RELOAD_RETRY_DELAY,
    )
    if notify:
        try:
            import telegram_links as tg_links

            tg_links.notify_user(
                user_id,
                tg_links.format_bot_event_ru(
                    action="reload",
                    bot=bot,
                    user_id=user_id,
                    detail="Конфиг перезагружен",
                ),
                kind="bot_reload",
            )
        except Exception:
            pass
    return result


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
            max_trades = INF_MAX_OPEN_TRADES
        try:
            max_trades = int(load_bot_limits().get(bot, max_trades))
        except (TypeError, ValueError):
            pass
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
        api_user, api_pass, _jwt = tm.tenant_api_creds(str(user["id"]))
    else:
        api_user, api_pass = AUTH_USER, AUTH_PASS
    token = base64.b64encode(f"{api_user}:{api_pass}".encode()).decode()
    return f"Basic {token}"


def _strategy_ids() -> set[str]:
    return {s["id"] for s in AVAILABLE_STRATEGIES}


def get_strategy_catalog(*, for_management: bool = False) -> list[dict]:
    """Catalog for UI. Sorted by num ascending; strategies without num go last.

    Effective test_group / ui_panel come from per-user placement.
    Demo sees only test panel; Live only main (unless for_management=True for
    Live «Добавить в Основные» list).
    """
    apply_hidden_disable_once()
    placement = ensure_strategy_ui_placement()
    rows: list[dict] = []
    for s in AVAILABLE_STRATEGIES:
        row = dict(s)
        panel = effective_ui_panel(s["id"], placement)
        row["ui_panel"] = panel
        row["in_main"] = s["id"] in set(placement.get("main") or [])
        row["catalog_test_group"] = bool(s.get("test_group"))
        row["test_group"] = panel == "test" or (panel == "htf1h" and not row["in_main"])
        rows.append(row)
    user = tc.current_user()
    allow = tm.user_allowed_strategies(user) if user else None
    if allow is not None:
        allow_set = set(allow)
        rows = [s for s in rows if s["id"] in allow_set]

    # Admin and demo see the test side. Live users see only what they put in main.
    if user and not for_management:
        main_ids = set(placement.get("main") or [])
        admin_home = tm.is_admin(user) and not tm.is_impersonating(user)

        def _on_test_side(row: dict) -> bool:
            panel = row.get("ui_panel")
            if panel == "test":
                return True
            return panel == "htf1h" and row["id"] not in main_ids

        def _on_main_side(row: dict) -> bool:
            panel = row.get("ui_panel")
            if panel == "main":
                return True
            return panel == "htf1h" and row["id"] in main_ids

        if admin_home or tm.is_demo_account(user):
            rows = [s for s in rows if _on_test_side(s)]
        elif tm.is_live_account(user):
            rows = [s for s in rows if _on_main_side(s)]

    def _sort_key(s: dict) -> tuple:
        has_num = s.get("num") is not None
        num = int(s["num"]) if has_num else 10_000
        return (0 if has_num else 1, num, s["id"])

    rows.sort(key=_sort_key)
    return rows


def get_strategy_catalog_for_promote() -> list[dict]:
    """Full catalog for Live «Добавить в Основные» (not yet in main)."""
    placement = ensure_strategy_ui_placement()
    main = set(placement.get("main") or [])
    rows = get_strategy_catalog(for_management=True)
    return [s for s in rows if s["id"] not in main]


def validate_strategy(strategy_id: str) -> str:
    strategy_id = strategy_id.strip()
    if strategy_id not in _strategy_ids():
        raise ValueError("unknown strategy")
    user = tc.current_user()
    if user and not tm.user_may_use_strategy(user, strategy_id):
        raise PermissionError(f"strategy not allowed: {strategy_id}")
    path = STRATEGIES_DIR / f"{strategy_id}.py"
    if not path.is_file():
        raise ValueError(f"strategy file missing: {strategy_id}")
    return strategy_id


# 1h SL/TP 13% study, 2026-04-01..2026-09-27, offline LightGBM gate >= 0.55.
# Everything else stays in simulation only.
PROD_DEFAULT_STRATEGIES = frozenset({
    "PsaraFlipStrategy",
    "DonchianAdxVolComboStrategy",
    "ObvEmaCrossStrategy",
    "ElderRayStrategy",
    "HeikinAshiFlipStrategy",
    "HmaPpoAtrComboStrategy",
    "AdxMomentumStrategy",
    "BollingerRsiStrategy",
    "MacdEmaStrategy",
    "SupertrendStrategy",
    "TripleEmaStrategy",
    "FibPullbackStrategy",
})


def default_enabled_map() -> dict[str, bool]:
    return {s["id"]: s["id"] in PROD_DEFAULT_STRATEGIES for s in AVAILABLE_STRATEGIES}


def _object_map(value: Any) -> dict[str, Any]:
    """enabled_strategies.json fields must be objects; a bool/list here used to crash /strategies."""
    return value if isinstance(value, dict) else {}


def load_enabled_map() -> dict[str, bool]:
    result = default_enabled_map()
    uid = user_prefs.settings_owner_id()
    payload = user_prefs.load_enabled_payload(uid, base=BASE)
    enabled = _object_map(payload.get("enabled"))
    for sid in result:
        if sid in enabled:
            result[sid] = bool(enabled[sid])
    return result


def _normalize_entry_side(value: Any) -> str:
    """both | long | short. Unknown values fall back to both."""
    raw = str(value or "both").strip().lower()
    if raw in {"long", "buy"}:
        return "long"
    if raw in {"short", "sell"}:
        return "short"
    return "both"


def load_entry_side_map() -> dict[str, str]:
    """Per-strategy direction for the whole catalog. Missing key means both."""
    payload = user_prefs.load_enabled_payload(user_prefs.settings_owner_id(), base=BASE)
    stored = _object_map(payload.get("entry_sides") if isinstance(payload, dict) else None)
    ids = [str(s["id"]) for s in AVAILABLE_STRATEGIES]
    for sid in stored:
        key = str(sid)
        if key not in ids:
            ids.append(key)
    return {sid: _normalize_entry_side(stored.get(sid, "both")) for sid in ids}


def load_inverted_map() -> dict[str, bool]:
    result = {s["id"]: False for s in AVAILABLE_STRATEGIES}
    uid = user_prefs.settings_owner_id()
    payload = user_prefs.load_enabled_payload(uid, base=BASE)
    inverted = _object_map(payload.get("inverted"))
    for sid in result:
        if sid in inverted:
            result[sid] = bool(inverted[sid])
    return result


def _trained_risk_catalog() -> dict[str, dict[str, Any]]:
    """SL/ROI keyed by strategy class: prod pack + player_scenarios (test/seq bots)."""
    out: dict[str, dict[str, Any]] = {}
    pack_candidates = [
        BASE.parent / "simulation" / "config" / "prod_top30_pack.json",
        BASE / "simulation" / "config" / "prod_top30_pack.json",
        Path(__file__).resolve().parents[2] / "simulation" / "config" / "prod_top30_pack.json",
    ]
    for pack_path in pack_candidates:
        if not pack_path.is_file():
            continue
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for s in pack.get("strategies") or []:
            cls = s.get("class_name")
            if not cls:
                continue
            out[str(cls)] = {
                "stoploss": float(s.get("stoploss") or 0),
                "tp": float(s.get("tp") or 0),
                "minimal_roi": s.get("minimal_roi") or {"0": float(s.get("tp") or 0)},
                "scenario_id": s.get("scenario_id"),
                "min_profit_proba": float(s.get("min_profit_proba") or 0.55),
            }
        break

    # Test / seq-gate strategies live in player_scenarios, not the top30 pack.
    scenario_candidates = [
        BASE.parent / "simulation" / "config" / "player_scenarios.json",
        BASE / "simulation" / "config" / "player_scenarios.json",
        Path(__file__).resolve().parents[2] / "simulation" / "config" / "player_scenarios.json",
    ]
    for sc_path in scenario_candidates:
        if not sc_path.is_file():
            continue
        try:
            scenarios = json.loads(sc_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(scenarios, list):
            break
        for sc in scenarios:
            if not isinstance(sc, dict):
                continue
            cls = sc.get("strategy")
            if not cls or cls in out:
                continue
            sl = sc.get("stoploss")
            roi = sc.get("minimal_roi")
            if sl is None and not isinstance(roi, dict):
                continue
            tp = 0.0
            if isinstance(roi, dict) and roi:
                try:
                    tp = float(roi.get("0") or next(iter(roi.values())))
                except (TypeError, ValueError, StopIteration):
                    tp = 0.0
            out[str(cls)] = {
                "stoploss": float(sl or 0),
                "tp": float(tp),
                "minimal_roi": roi if isinstance(roi, dict) else {"0": float(tp)},
                "scenario_id": sc.get("id"),
                "min_profit_proba": float(sc.get("min_profit_proba") or 0.55),
            }
        break
    return out


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
    uid = user_prefs.settings_owner_id()
    data = user_prefs.load_enabled_payload(uid, base=BASE)
    stored = _object_map(data.get("ml_confidence") if isinstance(data, dict) else None)
    for sid in result:
        if sid in stored:
            try:
                result[sid] = _normalize_ml_confidence(float(stored[sid]))
            except (TypeError, ValueError):
                pass
    return result


def load_trained_risk_map() -> dict[str, bool]:
    """«Свой SL/TP» per strategy. Off unless the user turned it on."""
    result = {s["id"]: False for s in AVAILABLE_STRATEGIES}
    uid = user_prefs.settings_owner_id()
    data = user_prefs.load_enabled_payload(uid, base=BASE)
    stored = _object_map(data.get("trained_risk") if isinstance(data, dict) else None)
    for sid in result:
        if sid in stored:
            result[sid] = bool(stored[sid])
    return result


def _read_enabled_file() -> dict[str, Any]:
    uid = user_prefs.settings_owner_id()
    return dict(user_prefs.load_enabled_payload(uid, base=BASE))


def _normalize_strategy_pair_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        try:
            pair = normalize_pair(str(item))
        except ValueError:
            continue
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def load_strategy_pairs_map() -> dict[str, list[str]]:
    """Per-strategy ticker whitelist (cascade). Empty list = no pairs for that strategy."""
    result = {s["id"]: [] for s in AVAILABLE_STRATEGIES}
    uid = user_prefs.settings_owner_id()
    data = user_prefs.load_enabled_payload(uid, base=BASE)
    stored = _object_map(data.get("pairs") if isinstance(data, dict) else None)
    for sid in result:
        if sid in stored:
            result[sid] = _normalize_strategy_pair_list(stored.get(sid))
    return result


def strategy_pairs_cascade_enabled() -> bool:
    uid = user_prefs.settings_owner_id()
    data = user_prefs.load_enabled_payload(uid, base=BASE)
    if not isinstance(data, dict):
        return False
    if data.get("_pairs_cascade"):
        return True
    return bool(_object_map(data.get("pairs")))


def strategy_pairs_union(pairs_map: dict[str, list[str]] | None = None) -> list[str]:
    m = pairs_map if pairs_map is not None else load_strategy_pairs_map()
    seen: set[str] = set()
    out: list[str] = []
    for sid in sorted(m):
        for pair in m.get(sid) or []:
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    return out


def ensure_whitelist_covers_strategy_pairs(pairs_map: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Add cascade tickers into the shared StaticPairList (never shrink)."""
    needed = strategy_pairs_union(pairs_map)
    if not needed:
        return {"added": [], "pairs": list(user_prefs.load_strategy_whitelist(base=BASE).get("pairs") or [])}
    desired = user_prefs.load_strategy_whitelist(base=BASE)
    current = [str(p) for p in (desired.get("pairs") or []) if str(p).strip()]
    have = set(current)
    added = [p for p in needed if p not in have]
    if not added:
        return {"added": [], "pairs": current}
    merged = list(current) + added
    user_prefs.save_strategy_whitelist(merged, source="strategy_pairs", protected=True, base=BASE)
    try:
        sync_strategy_whitelist_configs(merged)
    except Exception:
        pass
    return {"added": added, "pairs": merged}


def save_enabled_map(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
    trained_risk: dict[str, bool] | None = None,
    ml_confidence: dict[str, float] | None = None,
    pairs: dict[str, list[str]] | None = None,
    entry_sides: dict[str, str] | None = None,
) -> None:
    uid = user_prefs.settings_owner_id()
    data = _read_enabled_file()
    data["enabled"] = enabled
    if inverted is not None:
        data["inverted"] = {sid: bool(inverted.get(sid, False)) for sid in enabled}
    else:
        prev = _object_map(data.get("inverted"))
        data["inverted"] = {sid: bool(prev.get(sid, False)) for sid in enabled}
    if trained_risk is not None:
        data["trained_risk"] = {sid: bool(trained_risk.get(sid, False)) for sid in enabled}
    else:
        prev_tr = _object_map(data.get("trained_risk"))
        data["trained_risk"] = {
            sid: bool(prev_tr.get(sid, False)) for sid in enabled
        }
    defaults_ml = _ml_confidence_defaults()
    if ml_confidence is not None:
        data["ml_confidence"] = {
            sid: _normalize_ml_confidence(
                float(ml_confidence.get(sid, defaults_ml.get(sid, 0.55)))
            )
            for sid in enabled
        }
    else:
        prev_ml = _object_map(data.get("ml_confidence"))
        data["ml_confidence"] = {}
        for sid in enabled:
            if sid in prev_ml:
                try:
                    data["ml_confidence"][sid] = _normalize_ml_confidence(float(prev_ml[sid]))
                except (TypeError, ValueError):
                    data["ml_confidence"][sid] = float(defaults_ml.get(sid, 0.55))
            else:
                data["ml_confidence"][sid] = float(defaults_ml.get(sid, 0.55))
    if pairs is not None:
        data["pairs"] = {
            sid: _normalize_strategy_pair_list(pairs.get(sid)) for sid in enabled
        }
        data["_pairs_cascade"] = True
    else:
        prev_pairs = _object_map(data.get("pairs"))
        # Do not invent empty allowlists — that would block every strategy until seed.
        if prev_pairs or data.get("_pairs_cascade"):
            data["pairs"] = {
                sid: _normalize_strategy_pair_list(prev_pairs.get(sid)) for sid in enabled
            }
            data["_pairs_cascade"] = True
        else:
            data.pop("pairs", None)
            data.pop("_pairs_cascade", None)
    prev_sides = _object_map(data.get("entry_sides"))
    side_source = prev_sides if entry_sides is None else {**prev_sides, **entry_sides}
    data["entry_sides"] = {
        str(sid): norm
        for sid, raw in side_source.items()
        if (norm := _normalize_entry_side(raw)) != "both"
    }
    user_prefs.save_enabled_payload(uid, data, base=BASE)
    save_strategies_prefs(
        enabled,
        inverted=data["inverted"],
        trained_risk=data["trained_risk"],
        ml_confidence=data["ml_confidence"],
        pairs=data.get("pairs"),
        entry_sides=data["entry_sides"],
    )
    try:
        ensure_whitelist_covers_strategy_pairs(data["pairs"])
    except Exception:
        pass


def apply_hidden_disable_once() -> dict[str, Any]:
    """On first placement use: turn off all hidden strategies so they cannot trade without UI."""
    placement = ensure_strategy_ui_placement()
    if placement.get("_hidden_disabled"):
        return placement
    enabled = load_enabled_map()
    changed = False
    for sid in placement.get("hidden") or []:
        if enabled.get(sid):
            enabled[sid] = False
            changed = True
    if changed:
        save_enabled_map(enabled)
    placement = save_strategy_ui_placement(
        {
            **placement,
            "_hidden_disabled": True,
        }
    )
    return placement


def promote_strategy_to_main(strategy_id: str) -> dict[str, Any]:
    """Move strategy into Основные for the current user (enabled=false)."""
    apply_hidden_disable_once()
    strategy_id = validate_strategy(strategy_id)
    placement = ensure_strategy_ui_placement()
    panel = effective_ui_panel(strategy_id, placement)
    if panel == "main":
        raise ValueError("strategy is already in main panel")
    main = [sid for sid in (placement.get("main") or []) if sid != strategy_id]
    main.append(strategy_id)
    hidden = [sid for sid in (placement.get("hidden") or []) if sid != strategy_id]
    placement = save_strategy_ui_placement(
        {
            "main": main,
            "hidden": hidden,
            "_note": placement.get("_note") or _PLACEMENT_NOTE,
            "_hidden_disabled": True,
        }
    )
    enabled = load_enabled_map()
    enabled[strategy_id] = False
    save_enabled_map(enabled)
    ensure_router_config()
    return {
        "strategy": strategy_id,
        "ui_panel": "main",
        "enabled": False,
        "ui_placement": ui_placement_payload(placement),
        **get_strategies_payload(),
    }


def demote_strategy_to_test(strategy_id: str) -> dict[str, Any]:
    """Remove a strategy from this user's Основные list (enabled=false).

    Regular strategies land in Тестовые. 1h strategies stay on the 1h panel
    (placement.main only marked them as «основные»); effective panel stays htf1h.
    """
    apply_hidden_disable_once()
    strategy_id = validate_strategy(strategy_id)
    placement = ensure_strategy_ui_placement()
    in_main = strategy_id in set(placement.get("main") or [])
    panel = effective_ui_panel(strategy_id, placement)
    if not in_main and panel != "main":
        raise ValueError(f"strategy is not in main panel (panel={panel})")
    main = [sid for sid in (placement.get("main") or []) if sid != strategy_id]
    hidden = [sid for sid in (placement.get("hidden") or []) if sid != strategy_id]
    placement = save_strategy_ui_placement(
        {
            "main": main,
            "hidden": hidden,
            "_note": placement.get("_note") or _PLACEMENT_NOTE,
            "_hidden_disabled": True,
        }
    )
    enabled = load_enabled_map()
    enabled[strategy_id] = False
    save_enabled_map(enabled)
    ensure_router_config()
    return {
        "strategy": strategy_id,
        "ui_panel": effective_ui_panel(strategy_id, placement),
        "enabled": False,
        "ui_placement": ui_placement_payload(placement),
        **get_strategies_payload(),
    }


def move_all_strategies_to_test() -> dict[str, Any]:
    """Admin: put every catalog strategy into the Тестовые panel (enabled unchanged)."""
    apply_hidden_disable_once()
    placement = ensure_strategy_ui_placement()
    placement = save_strategy_ui_placement(
        {
            "main": [],
            "hidden": [],
            "_note": _PLACEMENT_NOTE,
            "_hidden_disabled": True,
        }
    )
    ensure_router_config()
    payload = ui_placement_payload(placement)
    return {
        "ok": True,
        "ui_panel": "test",
        "ui_placement": payload,
        "test_count": len(payload.get("test") or []),
        **get_strategies_payload(),
    }


def save_strategies_prefs(
    enabled: dict[str, bool],
    *,
    inverted: dict[str, bool] | None = None,
    trained_risk: dict[str, bool] | None = None,
    ml_confidence: dict[str, float] | None = None,
    pairs: dict[str, list[str]] | None = None,
    entry_sides: dict[str, str] | None = None,
) -> None:
    bot_strategies_file().parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"enabled": enabled}
    old: dict[str, Any] = {}
    if bot_strategies_file().is_file():
        try:
            old = json.loads(bot_strategies_file().read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
    if inverted is not None:
        payload["inverted"] = inverted
    elif "inverted" in old:
        payload["inverted"] = old["inverted"]
    if trained_risk is not None:
        payload["trained_risk"] = trained_risk
    elif "trained_risk" in old:
        payload["trained_risk"] = old["trained_risk"]
    if ml_confidence is not None:
        payload["ml_confidence"] = ml_confidence
    elif "ml_confidence" in old:
        payload["ml_confidence"] = old["ml_confidence"]
    if pairs is not None:
        payload["pairs"] = pairs
    elif "pairs" in old:
        payload["pairs"] = old["pairs"]
    if entry_sides is not None:
        payload["entry_sides"] = entry_sides
    elif "entry_sides" in old:
        payload["entry_sides"] = old["entry_sides"]
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
    stored = _object_map(data.get("enabled") if isinstance(data, dict) else None)
    result = default_enabled_map()
    for sid in result:
        if sid in stored:
            result[sid] = bool(stored[sid])
    inverted = load_inverted_map()
    stored_inv = _object_map(data.get("inverted") if isinstance(data, dict) else None)
    for sid in inverted:
        if sid in stored_inv:
            inverted[sid] = bool(stored_inv[sid])
    trained = load_trained_risk_map()
    stored_tr = _object_map(data.get("trained_risk") if isinstance(data, dict) else None)
    for sid in trained:
        if sid in stored_tr:
            trained[sid] = bool(stored_tr[sid])
    ml_conf = load_ml_confidence_map()
    stored_ml = _object_map(data.get("ml_confidence") if isinstance(data, dict) else None)
    for sid in ml_conf:
        if sid in stored_ml:
            try:
                ml_conf[sid] = _normalize_ml_confidence(float(stored_ml[sid]))
            except (TypeError, ValueError):
                pass
    pairs = load_strategy_pairs_map()
    stored_pairs = _object_map(data.get("pairs") if isinstance(data, dict) else None)
    for sid in pairs:
        if sid in stored_pairs:
            pairs[sid] = _normalize_strategy_pair_list(stored_pairs.get(sid))
    entry_sides = load_entry_side_map()
    stored_sides = _object_map(data.get("entry_sides") if isinstance(data, dict) else None)
    for sid in entry_sides:
        if sid in stored_sides:
            entry_sides[sid] = _normalize_entry_side(stored_sides.get(sid))
    save_enabled_map(
        result,
        inverted=inverted,
        trained_risk=trained,
        ml_confidence=ml_conf,
        pairs=pairs,
        entry_sides=entry_sides,
    )
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
    """0 = unlimited (only global max_open_trades). Any positive int allowed."""
    value = int(value)
    if value < 0:
        raise ValueError("max_open_trades_per_strategy must be >= 0 (0 = unlimited)")
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
    limits = load_bot_limits()
    save_bot_limits(limits, max_open_trades_per_strategy=value)
    _save_max_open_trades_per_strategy_file(value)
    try:
        record_max_open_trades_per_strategy(old, value)
    except Exception:
        pass
    # Router / trade-executor read DB (+ mirrored file) — no bot reload required.
    return {
        "max_open_trades_per_strategy": value,
        "reload_required": False,
        "limits": bot_limits_public_payload(),
        **get_strategies_payload(),
    }


def _normalize_test_strategy_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    src = dict(DEFAULT_TEST_STRATEGY_SETTINGS)
    if isinstance(raw, dict):
        src.update(raw)
    max_open = int(src.get("max_open_trades", DEFAULT_TEST_STRATEGY_SETTINGS["max_open_trades"]))
    if max_open < MIN_MAX_TRADES:
        raise ValueError(f"test max_open_trades must be >= {MIN_MAX_TRADES}")
    per = _normalize_max_open_trades_per_strategy(
        int(src.get("max_open_trades_per_strategy", 0))
    )
    stake = float(src.get("stake_amount", DEFAULT_TEST_STRATEGY_SETTINGS["stake_amount"]))
    if stake < MIN_STAKE_AMOUNT or stake > MAX_STAKE_AMOUNT:
        raise ValueError(f"test stake must be between {MIN_STAKE_AMOUNT} and {MAX_STAKE_AMOUNT}")
    sl = float(src.get("stoploss", DEFAULT_TEST_STRATEGY_SETTINGS["stoploss"]))
    tp = float(src.get("take_profit", DEFAULT_TEST_STRATEGY_SETTINGS["take_profit"]))
    sl = -abs(sl)
    tp = abs(tp)
    # Stored ratios: MIN_STRATEGY_STOPLOSS=-0.20 … MAX_STRATEGY_STOPLOSS=-0.01
    if not (MIN_TEST_STOPLOSS <= sl <= MAX_TEST_STOPLOSS):
        raise ValueError(
            f"test stoploss must be between {abs(MIN_TEST_STOPLOSS) * 100:g}% "
            f"and {abs(MAX_TEST_STOPLOSS) * 100:g}%"
        )
    if tp < MIN_TEST_TAKE_PROFIT or tp > MAX_TEST_TAKE_PROFIT:
        raise ValueError(
            f"test take_profit must be between {MIN_TEST_TAKE_PROFIT * 100:g}% "
            f"and {MAX_TEST_TAKE_PROFIT * 100:g}%"
        )
    return {
        "max_open_trades": max_open,
        "max_open_trades_per_strategy": per,
        "stake_amount": stake,
        "stoploss": sl,
        "take_profit": tp,
    }


def load_test_strategy_settings() -> dict[str, Any]:
    uid = user_prefs.settings_owner_id()
    raw = user_prefs.load_test_settings(uid, base=BASE)
    try:
        return _normalize_test_strategy_settings(raw if isinstance(raw, dict) else None)
    except ValueError:
        return dict(DEFAULT_TEST_STRATEGY_SETTINGS)


def test_strategy_settings_payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(settings or load_test_strategy_settings())
    sl = float(data["stoploss"])
    tp = float(data["take_profit"])
    return {
        "max_open_trades": int(data["max_open_trades"]),
        "max_open_trades_per_strategy": int(data["max_open_trades_per_strategy"]),
        "stake_amount": float(data["stake_amount"]),
        "stoploss": sl,
        "take_profit": tp,
        "stoploss_pct": round(abs(sl) * 100, 2),
        "take_profit_pct": round(tp * 100, 2),
    }


def _save_test_strategy_settings_file(settings: dict[str, Any]) -> None:
    uid = user_prefs.settings_owner_id()
    payload = _normalize_test_strategy_settings(settings)
    user_prefs.save_test_settings(uid, payload, base=BASE)


def set_test_strategy_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Update isolated test-block limits / stake / fallback SL-TP (no bot reload)."""
    current = load_test_strategy_settings()
    merged = dict(current)
    if "max_open_trades" in patch:
        merged["max_open_trades"] = int(patch["max_open_trades"])
    if "max_open_trades_per_strategy" in patch:
        merged["max_open_trades_per_strategy"] = int(patch["max_open_trades_per_strategy"])
    if "stake_amount" in patch:
        merged["stake_amount"] = float(patch["stake_amount"])
    if "stoploss_pct" in patch or "take_profit_pct" in patch:
        sl_pct = float(patch["stoploss_pct"]) if "stoploss_pct" in patch else abs(current["stoploss"]) * 100
        tp_pct = float(patch["take_profit_pct"]) if "take_profit_pct" in patch else abs(current["take_profit"]) * 100
        merged["stoploss"] = -abs(sl_pct) / 100.0
        merged["take_profit"] = abs(tp_pct) / 100.0
    if "stoploss" in patch:
        merged["stoploss"] = float(patch["stoploss"])
    if "take_profit" in patch:
        merged["take_profit"] = float(patch["take_profit"])
    normalized = _normalize_test_strategy_settings(merged)
    _save_test_strategy_settings_file(normalized)
    # Keep strategy bot ceiling ≥ test-block max so executor/ctengine don't clip earlier.
    bumped = False
    try:
        limits = load_bot_limits()
        test_max = int(normalized["max_open_trades"])
        if test_max > int(limits.get("strategy", 0)):
            limits["strategy"] = test_max
            save_bot_limits(limits)
            apply_bot_limits_to_configs()
            bumped = True
    except Exception:  # noqa: BLE001
        _server_log.warning("could not bump strategy max_open_trades to test max", exc_info=True)
    return {
        "test_settings": test_strategy_settings_payload(normalized),
        "reload_required": False,
        "strategy_max_bumped": bumped,
        "strategy_max_open_trades": int(load_bot_limits().get("strategy", 0)),
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
    apply_hidden_disable_once()
    enabled = load_enabled_map()
    enabled_ids = [sid for sid, on in enabled.items() if on]
    dual_hedge = load_dual_hedge_enabled()
    inverted = load_inverted_map()
    trained_risk = load_trained_risk_map()
    catalog = _trained_risk_catalog()
    ml_confidence = load_ml_confidence_map()
    ml_defaults = _ml_confidence_defaults()
    pairs = load_strategy_pairs_map()
    placement = ensure_strategy_ui_placement()
    user = tc.current_user()
    payload: dict[str, Any] = {
        "router": ROUTER_STRATEGY,
        "enabled": enabled,
        "enabled_ids": enabled_ids,
        "enabled_count": len(enabled_ids),
        "strategies": get_strategy_catalog(),
        "risk": strategy_risk_payload(),
        "dual_hedge": dual_hedge,
        "max_open_trades_per_strategy": load_max_open_trades_per_strategy(),
        "test_settings": test_strategy_settings_payload(),
        "inverted": inverted,
        "trained_risk": trained_risk,
        "trained_risk_available": {sid: (sid in catalog) for sid in enabled},
        "trained_risk_specs": catalog,
        "ml_confidence": ml_confidence,
        "ml_confidence_defaults": ml_defaults,
        "ml_confidence_choices": list(ML_CONFIDENCE_CHOICES),
        "entry_sides": load_entry_side_map(),
        "entry_side_choices": ["both", "long", "short"],
        "sltp": load_sltp_map(),
        "sltp_defaults": sltp_defaults_map(),
        "sltp_choices": list(SL_CHOICES),
        "sl_choices": list(SL_CHOICES),
        "tp_choices": list(TP_CHOICES),
        "htf_stake_amount": load_htf_stake(),
        "pairs": pairs,
        "pairs_union_count": len(strategy_pairs_union(pairs)),
        "_pairs_cascade": strategy_pairs_cascade_enabled(),
        "ui_placement": ui_placement_payload(placement),
        "account_kind": tm.account_kind(user) if user else "admin",
    }
    # Settings menu: every strategy not yet in this user's main, including 1h.
    if user:
        payload["promote_candidates"] = get_strategy_catalog_for_promote()
        payload["management_catalog"] = get_strategy_catalog(for_management=True)
    return payload


def toggle_strategy(strategy_id: str, enabled: bool) -> dict[str, Any]:
    try:
        strategy_id = validate_strategy(strategy_id)
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc
    apply_hidden_disable_once()
    panel = effective_ui_panel(strategy_id)
    if panel == "hidden":
        raise ValueError("strategy is hidden from UI panels")
    user = tc.current_user()
    placement = ensure_strategy_ui_placement()
    in_main = strategy_id in set(placement.get("main") or [])
    admin_home = bool(user and tm.is_admin(user) and not tm.is_impersonating(user))
    if user and tm.is_live_account(user) and not admin_home:
        if panel == "test" or (panel == "htf1h" and not in_main):
            raise ValueError("добавьте стратегию в основные в своих настройках")
    if user and tm.is_demo_account(user) and not admin_home:
        if panel == "main" or (panel == "htf1h" and in_main):
            raise ValueError("main strategies are not available on demo account")
    state = load_enabled_map()
    # Allow all-off — no forced "at least one enabled" (was locking the UI).
    state[strategy_id] = bool(enabled)
    save_enabled_map(state)
    ensure_router_config()
    try:
        record_strategy_toggle(strategy_id, bool(enabled))
    except Exception:
        pass
    return {
        "strategy": strategy_id,
        "enabled": state[strategy_id],
        **get_strategies_payload(),
    }


def set_all_strategies_enabled(enabled: bool, *, group: str | None = None) -> dict[str, Any]:
    """Enable or disable strategies in bulk.

    group:
      None / "all" — disable-all pauses everything; enable-all = main panel pack (not test/hidden)
      "main" — only strategies currently in Strategies panel
      "test" — only strategies currently in Тестовые
    """
    apply_hidden_disable_once()
    placement = ensure_strategy_ui_placement()
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

    scope = (group or ("all" if not enabled else "main")).strip().lower()
    if scope not in ("all", "main", "test", "htf1h"):
        scope = "main" if enabled else "all"

    next_state: dict[str, bool] = dict(prev)
    user = tc.current_user()
    admin_home = bool(user and tm.is_admin(user) and not tm.is_impersonating(user))
    live_user = bool(user and tm.is_live_account(user) and not admin_home)
    demo_user = bool(user and tm.is_demo_account(user) and not admin_home)
    main_ids = set(placement.get("main") or [])
    for s in AVAILABLE_STRATEGIES:
        sid = s["id"]
        if user and not tm.user_may_use_strategy(user, sid):
            next_state[sid] = False
            continue
        panel = effective_ui_panel(sid, placement)
        is_test = panel == "test"
        is_main = panel == "main"
        is_htf = panel == "htf1h"
        if panel == "hidden":
            # Hidden never participate in enable-all; stay off when disabling all.
            if not enabled and scope == "all":
                next_state[sid] = False
            continue
        if scope == "main" and not is_main:
            continue
        if scope == "test" and not is_test:
            continue
        if scope == "htf1h" and not is_htf:
            continue
        if is_htf and scope == "htf1h":
            on_main = sid in main_ids
            if live_user and not on_main:
                continue
            if (admin_home or demo_user) and on_main:
                continue
        if not enabled:
            next_state[sid] = False
        elif sid in legacy_ids:
            next_state[sid] = False
        elif is_test or is_htf or is_main:
            next_state[sid] = True
        elif pack_ids:
            next_state[sid] = sid in pack_ids and not is_test
        else:
            next_state[sid] = not is_test
    # Preserve: when enabling main panel, do not force-enable test strategies
    if enabled and scope == "main":
        for s in AVAILABLE_STRATEGIES:
            sid = s["id"]
            if effective_ui_panel(sid, placement) != "test":
                continue
            if user and not tm.user_may_use_strategy(user, sid):
                next_state[sid] = False
                continue
            if sid in prev:
                next_state[sid] = bool(prev.get(sid))
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
        "group": scope,
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
    enabled = load_enabled_map()
    state = load_trained_risk_map()
    state[strategy_id] = bool(trained_risk)
    save_enabled_map(enabled, trained_risk=state)
    return {
        "strategy": strategy_id,
        "trained_risk": state[strategy_id],
        **get_strategies_payload(),
    }


def sltp_defaults_map() -> dict[str, dict[str, int]]:
    """Shown until the user picks a stop/take for that strategy."""
    risk = load_strategy_risk()
    test = load_test_strategy_settings()
    placement = ensure_strategy_ui_placement()
    main = {
        "sl": snap_sl_pct(abs(float(risk["stoploss"])) * 100),
        "tp": snap_tp_pct(abs(float(risk["take_profit"])) * 100),
    }
    test_pair = {
        "sl": snap_sl_pct(abs(float(test["stoploss"])) * 100),
        "tp": snap_tp_pct(abs(float(test["take_profit"])) * 100),
    }
    out: dict[str, dict[str, int]] = {}
    for spec in AVAILABLE_STRATEGIES:
        sid = spec["id"]
        if sid in HTF1H_IDS:
            out[sid] = default_sltp(sid)
        elif effective_ui_panel(sid, placement) == "test":
            out[sid] = dict(test_pair)
        else:
            out[sid] = dict(main)
    return out


def load_sltp_map() -> dict[str, dict[str, int]]:
    """Explicit per-strategy stop/take only. Missing ids use sltp_defaults."""
    uid = user_prefs.settings_owner_id()
    payload = user_prefs.load_enabled_payload(uid, base=BASE)
    stored = _object_map(payload.get("sltp"))
    known = _strategy_ids()
    out: dict[str, dict[str, int]] = {}
    for sid, raw in stored.items():
        if sid not in known or not isinstance(raw, dict):
            continue
        if raw.get("sl") is None and raw.get("tp") is None:
            continue
        base = default_sltp(sid) if sid in HTF1H_IDS else {"sl": 5, "tp": 10}
        out[sid] = {
            "sl": snap_sl_pct(raw.get("sl", base["sl"])),
            "tp": snap_tp_pct(raw.get("tp", base["tp"])),
            "custom": True,
        }
    return out


def set_strategy_sltp(strategy_id: str, sl_pct: float, tp_pct: float) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    sl = snap_sl_pct(sl_pct)
    tp = snap_tp_pct(tp_pct)
    uid = user_prefs.settings_owner_id()
    payload = user_prefs.load_enabled_payload(uid, base=BASE)
    sltp = _object_map(payload.get("sltp"))
    sltp[strategy_id] = {"sl": sl, "tp": tp, "custom": True}
    payload["sltp"] = sltp
    user_prefs.save_enabled_payload(uid, payload, base=BASE)
    return {
        "strategy": strategy_id,
        "sltp": sltp[strategy_id],
        **get_strategies_payload(),
    }


def set_strategy_entry_side(strategy_id: str, side: str) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    raw = str(side or "").strip().lower()
    if raw not in {"both", "long", "short", "buy", "sell"}:
        raise ValueError("entry_side must be both, long, or short")
    chosen = _normalize_entry_side(raw)
    enabled = load_enabled_map()
    state = load_entry_side_map()
    old = state.get(strategy_id, "both")
    state[strategy_id] = chosen
    save_enabled_map(enabled, entry_sides=state)
    try:
        from grid_changelog import append_entry

        labels = {"both": "Оба", "long": "Лонг", "short": "Шорт"}
        label = next(
            (s.get("name") or strategy_id for s in AVAILABLE_STRATEGIES if s.get("id") == strategy_id),
            strategy_id,
        )
        append_entry(
            field="entry_side",
            old=labels.get(old, old),
            new=labels.get(chosen, chosen),
            category="strategy",
            source="ui",
            label=f"Направление ({label})",
            note=strategy_id,
        )
    except Exception:
        pass
    return {
        "strategy": strategy_id,
        "entry_side": chosen,
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
    old = state.get(strategy_id)
    state[strategy_id] = conf
    save_enabled_map(enabled, ml_confidence=state)
    try:
        from grid_changelog import append_entry

        label = next((s.get("name") or strategy_id for s in AVAILABLE_STRATEGIES if s.get("id") == strategy_id), strategy_id)
        append_entry(
            field="ml_gate_min_confidence",
            old=None if old is None else round(float(old) * 100),
            new=round(float(conf) * 100),
            category="strategy",
            source="ui",
            label=f"Уверенность ML ({label})",
            note=strategy_id,
        )
    except Exception:
        pass
    return {
        "strategy": strategy_id,
        "ml_confidence": state[strategy_id],
        **get_strategies_payload(),
    }


_ROBUST_FALLBACK_PAIRS = [
    "BCH/USDT:USDT",
    "SOL/USDT:USDT",
    "NOT/USDT:USDT",
    "APE/USDT:USDT",
    "ID/USDT:USDT",
    "AGLD/USDT:USDT",
    "MINA/USDT:USDT",
    "ZK/USDT:USDT",
    "ARPA/USDT:USDT",
    "CRV/USDT:USDT",
    "ETHFI/USDT:USDT",
    "BIO/USDT:USDT",
]


def _strategy_tickers_seed_path() -> Path:
    candidates = [
        BASE.parent / "simulation" / "results" / "strategy_tickers_seed.json",
        BASE / "simulation" / "results" / "strategy_tickers_seed.json",
        Path(__file__).resolve().parents[2] / "simulation" / "results" / "strategy_tickers_seed.json",
        BASE / "user_data" / "strategy_tickers_seed.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def set_strategy_pairs(strategy_id: str, pairs: list[str] | None) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    enabled = load_enabled_map()
    state = load_strategy_pairs_map()
    state[strategy_id] = _normalize_strategy_pair_list(pairs or [])
    save_enabled_map(enabled, pairs=state)
    return {
        "strategy": strategy_id,
        "pairs": state[strategy_id],
        **get_strategies_payload(),
    }


def add_strategy_pair(strategy_id: str, pair: str) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    pair = normalize_pair(pair)
    enabled = load_enabled_map()
    state = load_strategy_pairs_map()
    cur = list(state.get(strategy_id) or [])
    if pair not in cur:
        cur.append(pair)
    state[strategy_id] = cur
    save_enabled_map(enabled, pairs=state)
    return {
        "strategy": strategy_id,
        "pair": pair,
        "pairs": state[strategy_id],
        **get_strategies_payload(),
    }


def remove_strategy_pair(strategy_id: str, pair: str) -> dict[str, Any]:
    strategy_id = validate_strategy(strategy_id)
    pair = normalize_pair(pair)
    enabled = load_enabled_map()
    state = load_strategy_pairs_map()
    state[strategy_id] = [p for p in (state.get(strategy_id) or []) if p != pair]
    save_enabled_map(enabled, pairs=state)
    return {
        "strategy": strategy_id,
        "pair": pair,
        "pairs": state[strategy_id],
        **get_strategies_payload(),
    }


def seed_strategy_pairs_from_perf(*, only_empty: bool = False) -> dict[str, Any]:
    """Distribute per-strategy tickers from strategy_tickers_seed.json (live PnL analysis)."""
    path = _strategy_tickers_seed_path()
    seeded: dict[str, list[str]] = {}
    source = "robust_fallback"
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            block = raw.get("pairs") if isinstance(raw, dict) else None
            if isinstance(block, dict):
                seeded = {
                    str(k): _normalize_strategy_pair_list(v) for k, v in block.items()
                }
                source = str(raw.get("rule") or "seed_file")
        except (json.JSONDecodeError, OSError, TypeError):
            seeded = {}

    enabled = load_enabled_map()
    state = load_strategy_pairs_map()
    changed = 0
    for sid in enabled:
        if only_empty and state.get(sid):
            continue
        picked = list(seeded.get(sid) or [])
        if not picked:
            picked = list(_ROBUST_FALLBACK_PAIRS[:6])
        if state.get(sid) != picked:
            changed += 1
        state[sid] = picked
    save_enabled_map(enabled, pairs=state)
    wl = ensure_whitelist_covers_strategy_pairs(state)
    return {
        "ok": True,
        "source": source,
        "seed_file": str(path) if path.is_file() else None,
        "strategies_updated": changed,
        "pairs_union_count": len(strategy_pairs_union(state)),
        "whitelist_added": wl.get("added") or [],
        **get_strategies_payload(),
    }


def set_all_strategies_ml_confidence(
    confidence: float | None = None,
    *,
    reset: bool = False,
    group: str | None = None,
) -> dict[str, Any]:
    """Set the same ML confidence for catalog strategies, or restore pack defaults.

    group: None/"all" | "main" | "test"
    """
    enabled = load_enabled_map()
    defaults = _ml_confidence_defaults()
    prev = load_ml_confidence_map()
    scope = (group or "all").strip().lower()
    if scope not in ("all", "main", "test"):
        scope = "all"

    def _in_scope(sid: str) -> bool:
        panel = effective_ui_panel(sid)
        if scope == "main":
            return panel == "main"
        if scope == "test":
            return panel == "test"
        # all: visible panels only (never bulk-set hidden)
        return panel in ("main", "test")

    if reset:
        state = dict(prev)
        for sid, val in defaults.items():
            if _in_scope(sid):
                state[sid] = float(val)
        for s in AVAILABLE_STRATEGIES:
            sid = s["id"]
            if _in_scope(sid) and sid not in state:
                state[sid] = float(defaults.get(sid, 0.55))
        bulk_value: float | str = "default"
    else:
        if confidence is None:
            raise ValueError("ml_confidence required")
        raw = float(confidence)
        if raw > 1.0:
            raw = raw / 100.0
        conf = _normalize_ml_confidence(raw)
        state = dict(prev)
        for s in AVAILABLE_STRATEGIES:
            sid = s["id"]
            if _in_scope(sid):
                state[sid] = conf
        bulk_value = conf
    save_enabled_map(enabled, ml_confidence=state)
    try:
        from grid_changelog import append_note

        scope_ru = {"all": "все", "main": "основные", "test": "тестовые"}.get(scope, scope)
        if reset:
            text = f"Уверенность ML сброшена к default ({scope_ru} стратегии)"
        else:
            pct = round(float(bulk_value) * 100)
            text = f"Уверенность ML массово: {scope_ru} → {pct}%"
        append_note(text=text, category="strategy", source="ui")
    except Exception:
        pass
    return {
        "ml_confidence_bulk": bulk_value,
        "group": scope,
        **get_strategies_payload(),
    }


def get_state(bot: str) -> dict[str, Any]:
    cfg = load_config(CONFIGS[bot])
    config_pairs = list(cfg.get("exchange", {}).get("pair_whitelist", []))
    if bot == "strategy":
        # Prefer DB desired whitelist for UI (files can lag after a bad scan).
        try:
            db_pairs = list(user_prefs.load_strategy_whitelist(base=BASE).get("pairs") or [])
            if db_pairs:
                config_pairs = db_pairs
        except Exception:
            pass
    # Short timeouts — Finder/SE flaps must not block /pairs or settings UI.
    try:
        active = api_call(WHITELIST[bot], timeout=3.0, retries=1)
    except BaseException:
        active = {}
    try:
        black = api_call(BLACKLIST[bot], timeout=3.0, retries=1)
    except BaseException:
        black = {}
    max_trades = cfg.get("max_open_trades", 1)
    if max_trades == float("inf"):
        max_trades = INF_MAX_OPEN_TRADES
    # UI source of truth is SQLite bot_limits — never live show_config.
    limits_payload = load_bot_limits_file()
    try:
        max_trades = int(limits_payload["max_open_trades"].get(bot, max_trades))
    except (TypeError, ValueError, KeyError):
        pass
    try:
        stake = float(
            limits_payload.get("stake_amount", {}).get(
                bot, cfg.get("stake_amount", DEFAULT_GRID_STAKE if bot == "grid" else 5)
            )
        )
    except (TypeError, ValueError):
        stake = float(cfg.get("stake_amount", DEFAULT_GRID_STAKE if bot == "grid" else 5))
    state = {
        "bot": bot,
        "config_whitelist": config_pairs,
        "active_whitelist": active.get("whitelist", []),
        "blacklist": black.get("blacklist", []),
        "max_open_trades": int(max_trades),
        "stake_amount": stake,
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
        try:
            state["scan_policy"] = user_prefs.load_strategy_scan_policy(base=BASE)
        except Exception:
            state["scan_policy"] = {"apply_whitelist": False}
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
    """Persist max_open_trades to SQLite user_settings (source of truth).

    Config JSON is write-through for ctbot (Finder/Grid). Shared signal-engine
    stays at max_open_trades=1; trade-executor reads DB. Live show_config is
    never the authority — UI must not wait on it.
    """
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        raise ValueError("bot must be finder, strategy, or grid")
    value = int(value)
    if value < MIN_MAX_TRADES:
        raise ValueError(f"max_open_trades must be >= {MIN_MAX_TRADES} (0 = bot stopped)")

    limits = load_bot_limits()
    old_val = int(limits.get(bot, DEFAULT_BOT_LIMITS.get(bot, 2)))
    limits[bot] = value
    save_bot_limits(limits)
    apply_bot_limits_to_configs()

    reloads, reload_warning = _soft_reload_bot_once(bot)
    # DB write is the commit; live mismatch is informational only.
    live_applied = True
    live_now = value

    reload_result: Any = {
        "reloads": reloads,
        "live_applied": live_applied,
        "source": "user_settings.bot_limits",
    }

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
        trade_action = "start"
        try:
            trade_result = start_bot(bot)
        except BaseException as exc:  # noqa: BLE001
            trade_warning = str(exc)

    result: dict[str, Any] = {
        "bot": bot,
        "max_open_trades": value,
        "reloaded": reload_result,
        "live_applied": True,
        "live_max_open_trades": live_now,
        "state": {"bot": bot, "max_open_trades": value},
        "limits": bot_limits_public_payload(),
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
    # Enqueue ONLY on 0↔N — never restart because API is briefly wedged.
    try:
        uid = "admin"
        user = tc.current_user()
        if user and not (tm.is_admin(user) and not tm.is_impersonating(user)):
            uid = str(user.get("id") or "admin")
        control_plane.ensure_init()
        want = value >= 1
        snap = control_plane.snapshot_bot(uid, bot)
        prev_want = bool(snap.get("desired") if snap.get("desired") is not None else snap.get("enabled"))
        need_enqueue = want != prev_want
        control_plane.set_desired(
            uid,
            bot,
            want,
            updated_by="set_max_open_trades",
            enqueue=need_enqueue,
        )
        user_trading.save_trading_flags(uid, {bot: want})
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

    default_stake = DEFAULT_STAKES.get(bot, 5)
    limits = load_bot_limits()
    stakes = dict(load_bot_limits_file().get("stake_amount", DEFAULT_STAKES))
    old_val = float(stakes.get(bot, default_stake))
    stakes[bot] = value
    save_bot_limits(limits, stakes=stakes)
    apply_bot_limits_to_configs()
    reloads, reload_warning = _soft_reload_bot_once(bot)
    result: dict[str, Any] = {
        "bot": bot,
        "stake_amount": value,
        "reloaded": {"reloads": reloads},
        "limits": bot_limits_public_payload(),
        "state": {"bot": bot, "stake_amount": value, "max_open_trades": int(limits.get(bot, 0))},
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


# Primary path first (VPS systemd --logfile); later entries are local fallbacks.
LOG_SOURCES: dict[str, tuple[str, tuple[Path, ...]]] = {
    "executor": (
        "Исполнитель",
        (BASE / "user_data" / "logs" / "trade-executor.log",),
    ),
    "llm": (
        "DeepSeek",
        (BASE / "user_data" / "logs" / "deepseek.log",),
    ),
    "llm50": (
        "DeepSeek 50",
        (BASE / "user_data" / "logs" / "deepseek50.log",),
    ),
    "cursor": (
        "Cursor",
        (BASE / "user_data" / "logs" / "cursor.log",),
    ),
    "cursor50": (
        "Cursor 50",
        (BASE / "user_data" / "logs" / "cursor50.log",),
    ),
    "finder": (
        "ML Finder",
        (
            BASE / "user_data" / "logs" / "cryptotools-finder.log",
            BASE / "user_data" / "logs" / "cryptotools-finder.err.log",
        ),
    ),
    "strategy": (
        "Стратегии",
        (
            BASE / "user_data" / "logs" / "cryptotools-strategy.log",
            BASE / "user_data" / "logs" / "cryptotools-signal-engine.log",
            BASE / "user_data" / "logs" / "signal-engine.err.log",
        ),
    ),
    "grid": (
        "Grid",
        (
            BASE / "user_data" / "logs" / "cryptotools-grid.log",
            BASE / "user_data" / "logs" / "cryptotools-grid.err.log",
        ),
    ),
    "scanner": ("Сканер Grid", (BASE / "user_data" / "logs" / "ranging-scanner.log",)),
    "strategy_scanner": (
        "Сканер страт.",
        (BASE / "user_data" / "logs" / "strategy-scanner.log",),
    ),
    "pair_config": (
        "UI API",
        (
            BASE / "user_data" / "logs" / "pair-config.log",
            BASE / "user_data" / "logs" / "pair-config.err.log",
        ),
    ),
}


# Finder, Grid and the Grid scanner are retired: keep the files, omit them from the tab.
RETIRED_LOG_BOTS = frozenset({"finder", "grid", "scanner", "strategy_scanner"})


def _resolve_log_bots(requested: str) -> list[str]:
    if not requested or requested == "all":
        return [bot for bot in LOG_SOURCES if bot not in RETIRED_LOG_BOTS]
    bots = [b.strip() for b in requested.split(",") if b.strip()]
    return [b for b in bots if b in LOG_SOURCES and b not in RETIRED_LOG_BOTS]


def _pick_log_path(candidates: tuple[Path, ...]) -> Path:
    """Prefer a non-empty existing file; else the primary (VPS) path."""
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return candidates[0]


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
        label, candidates = LOG_SOURCES[bot]
        path = _pick_log_path(candidates)
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

    # Mixed sources: keep chronological order in the UI.
    entries.sort(key=lambda e: (e.get("ts") or "", e.get("bot") or "", e.get("line") or ""))
    return {"entries": entries, "positions": positions}


RANGING_PAIRS_FILE = BASE / "user_data" / "ranging_pairs.json"
SCAN_SCRIPT = BASE / "scripts" / "scan_ranging_pairs.py"
SCAN_LOCK = BASE / "user_data" / ".ranging_scan.lock"
RANGING_SCAN_LOG = BASE / "user_data" / "logs" / "ranging-scanner.log"
STRATEGY_PAIRS_FILE = BASE / "user_data" / "strategy_pairs.json"
STRATEGY_SCAN_SCRIPT = BASE / "scripts" / "scan_strategy_pairs.py"
STRATEGY_SCAN_LOCK = BASE / "user_data" / ".strategy_scan.lock"
STRATEGY_SCAN_LOG = BASE / "user_data" / "logs" / "strategy-scanner.log"


def _venv_python() -> Path:
    """Resolve site venv interpreter (Windows Scripts/ vs Linux bin/)."""
    candidates = (
        BASE / ".venv" / "Scripts" / "python.exe",
        BASE / ".venv" / "bin" / "python3",
        BASE / ".venv" / "bin" / "python",
    )
    for path in candidates:
        if path.is_file():
            return path
    return Path(sys.executable)


def _scan_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CT_BASE"] = str(BASE)
    if not env.get("CT_ENV"):
        for candidate in (
            BASE / ".env",
            BASE / ".cryptotools.env",
            Path("/home/cryptotools/.cryptotools.env"),
        ):
            if candidate.is_file():
                env["CT_ENV"] = str(candidate)
                break
    return env


def _append_scanner_log(log_path: Path, text: str, *, header: str | None = None) -> None:
    """Write scan stdout/stderr into the file the UI log viewer tails."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=APP_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+3")
    chunks: list[str] = []
    if header:
        chunks.append(f"{now} === {header} ===")
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # Keep lines that already have a timestamp prefix.
        if _LOG_TS_RE.match(line):
            chunks.append(line)
        else:
            chunks.append(f"{now} {line}")
    if not chunks:
        return
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(chunks) + "\n")
        fh.flush()


def _run_scan_script(script: Path, log_path: Path, *, label: str) -> subprocess.CompletedProcess[str]:
    """Run scanner and stream stdout/stderr into the UI log file live."""
    py = _venv_python()
    _append_scanner_log(log_path, "", header=f"{label} start - {py.name}")
    stdout_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            [str(py), "-u", str(script), "-v"],
            cwd=str(BASE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_scan_subprocess_env(),
        )
    except FileNotFoundError as exc:
        msg = f"python not found: {py}"
        _append_scanner_log(log_path, msg, header=f"{label} failed")
        raise RuntimeError(msg) from exc

    deadline = time.time() + 600.0
    assert proc.stdout is not None
    try:
        while True:
            if time.time() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                _append_scanner_log(log_path, "timeout after 600s", header=f"{label} timeout")
                raise RuntimeError(f"{label}: timeout after 600s")
            line = proc.stdout.readline()
            if line:
                stdout_chunks.append(line)
                _append_scanner_log(log_path, line.rstrip("\r\n"))
                continue
            if proc.poll() is not None:
                rest = proc.stdout.read() or ""
                if rest:
                    stdout_chunks.append(rest)
                    _append_scanner_log(log_path, rest)
                break
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    code = int(proc.returncode or 0)
    combined = "".join(stdout_chunks)
    if not combined.strip():
        _append_scanner_log(log_path, f"(no output, exit={code})", header=f"{label} done")
    if code != 0:
        _append_scanner_log(log_path, f"exit={code}", header=f"{label} failed")
    return subprocess.CompletedProcess(
        args=[str(py), str(script), "-v"],
        returncode=code,
        stdout=combined,
        stderr="",
    )


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

    try:
        proc = _run_scan_script(SCAN_SCRIPT, RANGING_SCAN_LOG, label="ranging scan")
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


def _apply_strategy_scan_to_signal_engine(
    whitelist: list[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Write scan whitelist into live configs and reload signal-engine (port 8081).

    Source of truth for the desired list is control_plane user_settings
    (admin/strategy_pair_whitelist). Auto-apply is gated by strategy_scan_policy
    and a hard shrink guard — scanners must not silently collapse a large WL.
    """
    pairs = [str(p).strip() for p in (whitelist or []) if str(p).strip()]
    if not pairs:
        return {"applied": False, "reason": "empty whitelist", "reload": False}

    policy = user_prefs.load_strategy_scan_policy(base=BASE)
    desired = user_prefs.load_strategy_whitelist(base=BASE)
    current = list(desired.get("pairs") or [])
    # Prefer live config size if somehow larger than DB (heal path).
    for rel in ("user_data/config_signal_engine.json", "user_data/config_strategy.json"):
        path = BASE / rel
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            wl = list(cfg.get("exchange", {}).get("pair_whitelist") or [])
            if len(wl) > len(current):
                current = wl
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    min_keep = int(policy.get("min_keep_pairs") or 40)
    allow_shrink = bool(policy.get("allow_shrink"))
    if not force and not allow_shrink and len(current) >= min_keep:
        if len(pairs) < max(min_keep, int(len(current) * 0.5)):
            reason = (
                f"refuse shrink {len(current)}→{len(pairs)} "
                f"(DB protected; allow_shrink=false, min_keep={min_keep})"
            )
            _append_scanner_log(
                STRATEGY_SCAN_LOG,
                reason,
                header="strategy scan apply→signal-engine",
            )
            return {
                "applied": False,
                "reason": reason,
                "pairs": len(pairs),
                "current_pairs": len(current),
                "reload": False,
            }

    updated: list[str] = []
    for rel in (
        "user_data/config_signal_engine.json",
        "user_data/config_strategy.json",
    ):
        path = BASE / rel
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        old = list(cfg.get("exchange", {}).get("pair_whitelist") or [])
        if old == pairs:
            continue
        cfg.setdefault("exchange", {})["pair_whitelist"] = pairs
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        updated.append(rel)

    # Persist desired list in SQLite so the next scan/restart cannot lose it.
    user_prefs.save_strategy_whitelist(pairs, source="scan_apply", protected=True, base=BASE)

    reload_ok = False
    reload_err: str | None = None
    try:
        from scan_strategy_pairs import reload_strategy  # noqa: WPS433

        env = _scan_subprocess_env()
        api_user = (
            os.environ.get("FREQUI_USERNAME")
            or env.get("FREQUI_USERNAME")
            or "cryptotools"
        )
        api_pass = os.environ.get("FREQUI_PASSWORD") or env.get("FREQUI_PASSWORD") or ""
        if not api_pass:
            ct_env = Path(env.get("CT_ENV") or "")
            if ct_env.is_file():
                for line in ct_env.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == "FREQUI_USERNAME":
                        api_user = v or api_user
                    elif k == "FREQUI_PASSWORD":
                        api_pass = v
        if api_pass:
            reload_ok = bool(
                reload_strategy(
                    "http://127.0.0.1:8081/api/v1/reload_config",
                    api_user,
                    api_pass,
                )
            )
            if not reload_ok:
                reload_err = "reload_config returned false"
        else:
            reload_err = "FREQUI_PASSWORD missing"
    except Exception as exc:  # noqa: BLE001
        reload_err = str(exc)[:200]

    _append_scanner_log(
        STRATEGY_SCAN_LOG,
        (
            f"apply whitelist n={len(pairs)} updated={updated or ['unchanged']} "
            f"reload={'ok' if reload_ok else 'fail'}"
            + (f" ({reload_err})" if reload_err else "")
        ),
        header="strategy scan apply→signal-engine",
    )
    return {
        "applied": True,
        "pairs": len(pairs),
        "updated_configs": updated,
        "reload": reload_ok,
        "reload_error": reload_err,
    }


def _strategy_scan_apply_from_config() -> bool:
    """DB policy first; JSON strategy_scan_config is a mirror."""
    return bool(user_prefs.load_strategy_scan_policy(base=BASE).get("apply_whitelist"))


def sync_strategy_whitelist_configs(pairs: list[str]) -> list[str]:
    """Write desired pairs into signal-engine + strategy JSON (engine reads files)."""
    cleaned = [str(p).strip() for p in pairs if str(p).strip()]
    updated: list[str] = []
    for rel in (
        "user_data/config_signal_engine.json",
        "user_data/config_strategy.json",
    ):
        path = BASE / rel
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        old = list(cfg.get("exchange", {}).get("pair_whitelist") or [])
        if old == cleaned:
            continue
        cfg.setdefault("exchange", {})["pair_whitelist"] = cleaned
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        updated.append(rel)
    return updated


def ensure_strategy_whitelist_from_db() -> dict[str, Any]:
    """On startup: seed/heal Strategy WL from SQLite; never leave a shrunk JSON as truth."""
    policy = user_prefs.save_strategy_scan_policy(
        {"apply_whitelist": False, "allow_shrink": False},
        base=BASE,
    )
    desired = user_prefs.load_strategy_whitelist(base=BASE)
    pairs = list(desired.get("pairs") or [])
    if not pairs:
        return {"ok": False, "reason": "empty", "policy": policy, "pairs": 0}
    updated = sync_strategy_whitelist_configs(pairs)
    return {
        "ok": True,
        "pairs": len(pairs),
        "source": desired.get("source"),
        "synced": updated,
        "policy": policy,
    }


def trigger_strategy_scan(*, apply_whitelist: bool | None = None) -> dict[str, Any]:
    # None → DB policy (adaptive must NOT overwrite a large WL).
    if apply_whitelist is None:
        apply_whitelist = _strategy_scan_apply_from_config()
    if STRATEGY_SCAN_LOCK.is_file():
        age = time.time() - STRATEGY_SCAN_LOCK.stat().st_mtime
        if age < 600:
            raise ValueError("Скан уже выполняется — подождите завершения")
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)

    STRATEGY_SCAN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_SCAN_LOCK.write_text(str(int(time.time())), encoding="utf-8")

    try:
        proc = _run_scan_script(STRATEGY_SCAN_SCRIPT, STRATEGY_SCAN_LOG, label="strategy scan")
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
        status = get_strategy_scan_status()
        try:
            if status.get("whitelist"):
                record_strategy_scan_summary(status)
        except Exception:
            pass
        apply_info: dict[str, Any] = {
            "applied": False,
            "skipped": True,
            "reason": "apply_whitelist=false (DB policy)",
        }
        if apply_whitelist:
            apply_info = _apply_strategy_scan_to_signal_engine(
                list(status.get("whitelist") or summary.get("whitelist") or [])
            )
        else:
            _append_scanner_log(
                STRATEGY_SCAN_LOG,
                "skip apply→signal-engine (DB apply_whitelist=false)",
                header="strategy scan apply→signal-engine",
            )
            # Heal: if JSON was shrunk earlier, restore from DB.
            ensure_strategy_whitelist_from_db()
        return {
            **status,
            "ok": True,
            "summary": summary,
            "apply": apply_info,
        }
    finally:
        STRATEGY_SCAN_LOCK.unlink(missing_ok=True)


def _llm_open_trades(user_id: str) -> list[dict[str, Any]]:
    del user_id
    return load_open_trades_from_db("strategy", llm_mode="only")


_LLM_UP = {"ok": False, "checked_at": 0.0}
_LLM_UP_LOCK = threading.Lock()
_LLM_UP_TTL_SEC = 45.0


def _llm_endpoint_up() -> bool:
    """Cached reachability of the DeepSeek gateway. The UI polls this often."""
    now = time.monotonic()
    with _LLM_UP_LOCK:
        if _LLM_UP["checked_at"] and now - float(_LLM_UP["checked_at"]) < _LLM_UP_TTL_SEC:
            return bool(_LLM_UP["ok"])
    ok = False
    try:
        app_root = Path(__file__).resolve().parents[1]
        for candidate in (app_root, app_root.parent):
            if (candidate / "simulation" / "strategies" / "deepseek_openrouter.py").is_file():
                if str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
                break
        from simulation.strategies.deepseek_openrouter import endpoint_ready

        ok = bool(endpoint_ready())
    except Exception:
        _server_log.exception("llm endpoint probe failed")
        ok = False
    with _LLM_UP_LOCK:
        _LLM_UP["ok"] = ok
        _LLM_UP["checked_at"] = time.monotonic()
    return ok


def _llm_bot_view(user_id: str) -> dict[str, Any]:
    import llm_settings
    import user_trading

    settings = llm_settings.load(user_id)
    status: dict[str, Any] = {}
    status_path = BASE / "user_data" / "llm_bot_status.json"
    if status_path.is_file():
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                status = raw
        except (OSError, json.JSONDecodeError):
            status = {}
    floor = float(settings["min_confidence"])
    decisions = status.get("decisions")
    if isinstance(decisions, list):
        visible: list[dict[str, Any]] = []
        for row in decisions:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            if item.get("state") == "signal":
                try:
                    conf = float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    conf = 0.0
                if conf < floor:
                    item["state"] = "flat"
                    item["side"] = "hold"
            visible.append(item)
        status = {**status, "decisions": visible}
    return {
        "enabled": user_trading.is_trading_enabled(user_id, "llm"),
        "settings": settings,
        "status": status,
        "openseek_up": _llm_endpoint_up(),
        "trades": _llm_open_trades(user_id),
        "pairs": list(settings.get("pairs") or []),
    }


def _llm_bot_update(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    import llm_settings
    import user_trading

    patch: dict[str, Any] = {}
    if "take_profit_pct" in data or "take_profit" in data:
        patch["take_profit_pct" if "take_profit_pct" in data else "take_profit"] = data.get(
            "take_profit_pct", data.get("take_profit")
        )
    if "max_open_trades" in data:
        patch["max_open_trades"] = data.get("max_open_trades")
    if "stake_amount" in data:
        patch["stake_amount"] = data.get("stake_amount")
    if "min_confidence_pct" in data or "min_confidence" in data:
        if "min_confidence_pct" in data:
            patch["min_confidence_pct"] = data.get("min_confidence_pct")
        else:
            patch["min_confidence"] = data.get("min_confidence")
    if "pairs" in data:
        patch["pairs"] = data.get("pairs")
    if patch:
        llm_settings.save(user_id, patch)
    saved = llm_settings.load(user_id)
    cap = int(saved["max_open_trades"])
    pair_list = list(saved["pairs"])
    if "enabled" in data:
        enabled = bool(data.get("enabled"))
        if enabled and cap <= 0:
            raise ValueError("макс. открытых = 0 — поставьте хотя бы 1")
        if enabled and not pair_list:
            raise ValueError("добавьте хотя бы одну пару")
        user_trading.save_trading_flags(user_id, {"llm": enabled})
    elif user_trading.is_trading_enabled(user_id, "llm") and (cap <= 0 or not pair_list):
        user_trading.save_trading_flags(user_id, {"llm": False})
    return _llm_bot_view(user_id)


def _llm50_bot_view(user_id: str) -> dict[str, Any]:
    import llm_book
    import user_trading

    db_path = llm_book.trades_db_for(user_id)
    status = llm_book.read_status(BASE)
    users = status.get("users") if isinstance(status.get("users"), dict) else {}
    mine = users.get(user_id) if isinstance(users.get(user_id), dict) else {}
    book_stats = llm_book.stats(db_path)
    return {
        "enabled": user_trading.is_trading_enabled(user_id, "llm50"),
        "budget_usdt": llm_book.BUDGET_USDT,
        "stats": book_stats,
        "status": {
            "state": mine.get("state") or status.get("state") or "idle",
            "reason": mine.get("reason") or status.get("reason") or "",
            "actions": mine.get("actions") or [],
        },
        "trades": load_open_trades_from_db("strategy", llm_mode="book"),
        "pairs": llm_book.pairs(),
    }


def _llm50_bot_update(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    import user_trading

    if "enabled" in data:
        user_trading.save_trading_flags(user_id, {"llm50": bool(data.get("enabled"))})
    return _llm50_bot_view(user_id)


def _cursor_health() -> dict[str, Any]:
    path = BASE / "user_data" / "cursor_health.json"
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError):
            pass
    key = (os.environ.get("CURSOR") or os.environ.get("CURSOR_API_KEY") or "").strip()
    return {"ok": bool(key), "reason": "" if key else "cursor token missing"}


def _cursor_endpoint_up() -> bool:
    return bool(_cursor_health().get("ok"))


def _cursor_bot_view(user_id: str) -> dict[str, Any]:
    import cursor_settings
    import user_trading

    settings = cursor_settings.load(user_id)
    status: dict[str, Any] = {}
    status_path = BASE / "user_data" / "cursor_bot_status.json"
    if status_path.is_file():
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                status = raw
        except (OSError, json.JSONDecodeError):
            status = {}
    floor = float(settings["min_confidence"])
    decisions = status.get("decisions")
    if isinstance(decisions, list):
        visible: list[dict[str, Any]] = []
        for row in decisions:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            if item.get("state") == "signal":
                try:
                    conf = float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    conf = 0.0
                if conf < floor:
                    item["state"] = "flat"
                    item["side"] = "hold"
            visible.append(item)
        status = {**status, "decisions": visible}
    return {
        "enabled": user_trading.is_trading_enabled(user_id, "cursor"),
        "settings": settings,
        "status": status,
        "openseek_up": _cursor_endpoint_up(),
        "health_reason": str(_cursor_health().get("reason") or ""),
        "trades": load_open_trades_from_db("strategy", llm_mode="cursor"),
        "pairs": list(settings.get("pairs") or []),
    }


def _cursor_bot_update(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    import cursor_settings
    import user_trading

    patch: dict[str, Any] = {}
    if "take_profit_pct" in data or "take_profit" in data:
        patch["take_profit_pct" if "take_profit_pct" in data else "take_profit"] = data.get(
            "take_profit_pct", data.get("take_profit")
        )
    if "max_open_trades" in data:
        patch["max_open_trades"] = data.get("max_open_trades")
    if "stake_amount" in data:
        patch["stake_amount"] = data.get("stake_amount")
    if "min_confidence_pct" in data or "min_confidence" in data:
        if "min_confidence_pct" in data:
            patch["min_confidence_pct"] = data.get("min_confidence_pct")
        else:
            patch["min_confidence"] = data.get("min_confidence")
    if "pairs" in data:
        patch["pairs"] = data.get("pairs")
    if patch:
        cursor_settings.save(user_id, patch)
    saved = cursor_settings.load(user_id)
    cap = int(saved["max_open_trades"])
    pair_list = list(saved["pairs"])
    if "enabled" in data:
        enabled = bool(data.get("enabled"))
        if enabled and cap <= 0:
            raise ValueError("макс. открытых = 0 — поставьте хотя бы 1")
        if enabled and not pair_list:
            raise ValueError("добавьте хотя бы одну пару")
        user_trading.save_trading_flags(user_id, {"cursor": enabled})
    elif user_trading.is_trading_enabled(user_id, "cursor") and (cap <= 0 or not pair_list):
        user_trading.save_trading_flags(user_id, {"cursor": False})
    return _cursor_bot_view(user_id)


def _cursor50_bot_view(user_id: str) -> dict[str, Any]:
    import cursor_book
    import user_trading

    db_path = cursor_book.trades_db_for(user_id)
    status = cursor_book.read_status(BASE)
    users = status.get("users") if isinstance(status.get("users"), dict) else {}
    mine = users.get(user_id) if isinstance(users.get(user_id), dict) else {}
    book_stats = cursor_book.stats(db_path)
    return {
        "enabled": user_trading.is_trading_enabled(user_id, "cursor50"),
        "budget_usdt": cursor_book.BUDGET_USDT,
        "stats": book_stats,
        "status": {
            "state": mine.get("state") or status.get("state") or "idle",
            "reason": mine.get("reason") or status.get("reason") or "",
            "actions": mine.get("actions") or [],
        },
        "trades": load_open_trades_from_db("strategy", llm_mode="cursor50"),
        "pairs": cursor_book.pairs(),
        "openseek_up": _cursor_endpoint_up(),
        "health_reason": str(_cursor_health().get("reason") or ""),
    }


def _cursor50_bot_update(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    import user_trading

    if "enabled" in data:
        user_trading.save_trading_flags(user_id, {"cursor50": bool(data.get("enabled"))})
    return _cursor50_bot_view(user_id)


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
            return tm.session_user_from_payload(payload)
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

    def _restart_strategy_bot(self) -> None:
        """Admin: restart the shared strategy signal-engine."""
        unit = "cryptotools-signal-engine.service"
        wrapper = BASE / "deploy" / "admin-systemctl.sh"
        cmd = (
            ["sudo", "-n", str(wrapper), "restart", unit]
            if wrapper.is_file()
            else ["systemctl", "restart", unit]
        )
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=150,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "restart timed out"})
            return
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        if cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "restart failed").strip()
            self._json(500, {"ok": False, "error": err})
            return
        state = self._unit_active("cryptotools-signal-engine")
        who = str((self.user or {}).get("username") or "admin")
        _server_log.info("strategy bot restart by %s -> %s", who, state)
        self._json(
            200,
            {"ok": True, "unit": "cryptotools-signal-engine", "state": state},
        )

    def _require_admin(self) -> bool:
        # Real admin only (not while impersonating a tenant).
        if (
            not self.user
            or not tm.is_admin(self.user)
            or tm.is_impersonating(self.user)
        ):
            self._json(403, {"error": "admin required"})
            return False
        return True

    def _can_manage_strategy_placement(self) -> bool:
        """Admin (real) or Live tenants may manage own placement. Demo is view-only."""
        if not self.user:
            self._json(401, {"error": "unauthorized"})
            return False
        if is_demo_mirror_user(self.user):
            self._json(
                403,
                {
                    "error": "demo_readonly",
                    "message": "Demo: только просмотр. Перенос стратегий — в Live.",
                },
            )
            return False
        if tm.is_admin(self.user) and not tm.is_impersonating(self.user):
            return True
        if tm.is_live_account(self.user):
            return True
        self._json(403, {"error": "placement manage not allowed"})
        return False

    def _require_writable(self) -> bool:
        """Demo mirror viewers cannot change settings / close trades / start bots."""
        if is_demo_mirror_user(self.user):
            self._json(
                403,
                {
                    "error": "demo_readonly",
                    "message": "Demo: сделки администратора только для просмотра",
                },
            )
            return False
        return True

    def _require_block(self, block_id: str) -> bool:
        if not tm.user_may_use_block(self.user, block_id):
            self._json(403, {"error": "block not allowed", "block": block_id})
            return False
        return True

    def _tcp_open(self, port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
        import socket

        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    def _http_ok(self, url: str, timeout: float = 1.5) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= int(resp.status) < 300
        except Exception:
            return False

    def _process_running_substr(self, needle: str) -> bool:
        """Best-effort check that a process cmdline contains needle (Windows/Linux)."""
        needle_l = needle.lower()
        try:
            if os.name == "nt":
                cp = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-CimInstance Win32_Process | "
                        "Select-Object -ExpandProperty CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                blob = (cp.stdout or "").lower()
                return needle_l in blob
            # Linux
            proc = Path("/proc")
            if proc.is_dir():
                for p in proc.iterdir():
                    if not p.name.isdigit():
                        continue
                    try:
                        cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                            "utf-8", "ignore"
                        ).lower()
                    except OSError:
                        continue
                    if needle_l in cmd:
                        return True
        except (OSError, subprocess.SubprocessError):
            return False
        return False

    def _heartbeat_fresh(self, path: Path, max_age_sec: float = 90.0) -> bool:
        try:
            if not path.is_file():
                return False
            age = time.time() - path.stat().st_mtime
            return age <= max_age_sec
        except OSError:
            return False

    def _unit_active(self, unit: str) -> str:
        if not unit:
            return "unknown"
        # Local/Windows: systemctl is absent — probe ports / processes / heartbeats.
        local_fallbacks = {
            "cryptotools-signal-engine": lambda: self._http_ok(
                "http://127.0.0.1:8081/api/v1/ping"
            )
            or self._tcp_open(8081),
            "cryptotools-strategy": lambda: self._http_ok(
                "http://127.0.0.1:8081/api/v1/ping"
            )
            or self._tcp_open(8081),
            "cryptotools-grid": lambda: self._http_ok("http://127.0.0.1:8082/api/v1/ping")
            or self._tcp_open(8082),
            "cryptotools-finder": lambda: self._http_ok("http://127.0.0.1:8080/api/v1/ping")
            or self._tcp_open(8080),
            "trade-executor": lambda: self._heartbeat_fresh(
                Path(os.environ.get("CT_BASE", str(BASE)))
                / "user_data"
                / "logs"
                / "trade-executor.heartbeat"
            )
            or self._process_running_substr("trade_executor.py"),
            "pair-config": lambda: self._tcp_open(
                int(os.environ.get("PAIR_CONFIG_PORT", "8090"))
            ),
        }
        try:
            cp = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            state = (cp.stdout or "").strip() or "unknown"
            # On Windows systemctl often missing → FileNotFound; if present but inactive,
            # still allow local fallbacks (dev PC).
            if state == "active":
                return "active"
            if unit in local_fallbacks and local_fallbacks[unit]():
                return "active"
            if state in ("inactive", "failed", "activating", "deactivating"):
                return state
            if unit in local_fallbacks:
                return "active" if local_fallbacks[unit]() else "inactive"
            return state
        except (OSError, subprocess.SubprocessError):
            if unit in local_fallbacks:
                return "active" if local_fallbacks[unit]() else "inactive"
            return "unknown"

    def _shared_stack_status(self) -> dict[str, Any]:
        signal_state = self._unit_active("cryptotools-signal-engine")
        exec_state = self._unit_active("trade-executor")
        return {
            "mode": "shared",
            "signal_engine": {
                "unit": "cryptotools-signal-engine",
                "active": signal_state == "active",
                "state": signal_state,
                "port": 8081,
            },
            "trade_executor": {
                "unit": "trade-executor",
                "active": exec_state == "active",
                "state": exec_state,
            },
        }

    def _control_uid(self) -> str:
        user = self.user or {}
        if tm.is_admin(user) and not tm.is_impersonating(user):
            return "admin"
        return str(user.get("id") or "")

    def _auth_me_payload(self) -> dict[str, Any]:
        user = self.user or {}
        public = tm._public_user(user) if user else {}
        uid = self._control_uid()
        try:
            control_plane.ensure_init()
            cp_snap = control_plane.snapshot_all(uid)
        except Exception:
            cp_snap = {"user_id": uid, "bots": {}}
        if tm.is_admin(user) and not tm.is_impersonating(user):
            secrets_st = tm.admin_secrets_status()
            shared = self._shared_stack_status()
            grid_state = self._unit_active("cryptotools-grid")
            finder_state = self._unit_active("cryptotools-finder")
            bots_running: dict[str, Any] = {
                "strategy": {
                    **shared["signal_engine"],
                    "label": "Signal Engine",
                },
                "grid": {
                    "unit": "cryptotools-grid",
                    "active": grid_state == "active",
                    "state": grid_state,
                    "port": 8082,
                    "label": "Grid",
                },
                "finder": {
                    "unit": "cryptotools-finder",
                    "active": finder_state == "active",
                    "state": finder_state,
                    "port": 8080,
                    "label": "ML Finder",
                },
            }
        else:
            secrets_st = tm.secrets_status(str(user["id"]))
            shared = self._shared_stack_status()
            flags = user_trading.load_trading_flags(str(user["id"]))
            grid_state = self._unit_active("cryptotools-grid")
            finder_state = self._unit_active("cryptotools-finder")
            bots_running = {
                "strategy": {
                    **shared["signal_engine"],
                    "user_trading": flags.get("strategy", False),
                },
                "grid": {
                    "unit": "cryptotools-grid",
                    "active": grid_state == "active",
                    "state": grid_state,
                    "port": 8082,
                    "user_trading": flags.get("grid", False),
                },
                "finder": {
                    "unit": "cryptotools-finder",
                    "active": finder_state == "active",
                    "state": finder_state,
                    "port": 8080,
                    "user_trading": flags.get("finder", False),
                },
            }
        # Prefer control-plane desired/status for active flags when present.
        for bname, brow in (cp_snap.get("bots") or {}).items():
            if bname in bots_running and isinstance(brow, dict):
                bots_running[bname]["control_status"] = brow.get("status")
                bots_running[bname]["desired"] = bool(brow.get("desired"))
                if brow.get("status") == "RUNNING":
                    bots_running[bname]["active"] = True
        flags = user_trading.load_trading_flags(uid)
        for bname, brow in (cp_snap.get("bots") or {}).items():
            if isinstance(brow, dict) and "desired" in brow:
                flags[bname] = bool(brow.get("desired"))
        payload: dict[str, Any] = {
            "user": public,
            "role": public.get("role"),
            "secrets": secrets_st,
            "bots_running": bots_running,
            "bots_control": cp_snap.get("bots") or {},
            "shared_stack": shared,
            "trading_flags": flags,
            "allowed_strategies": tm.user_allowed_strategies(user),
            "allowed_blocks": tm.user_allowed_blocks(user),
            "impersonating": tm.is_impersonating(user),
        }
        payload.update(tm.account_switch_info(user))
        if tm.is_impersonating(user):
            admin = tm.get_user_by_id(str(user.get("_imp_by") or ""))
            payload["impersonated_by"] = (
                {"id": admin.get("id"), "username": admin.get("username")}
                if admin
                else {"id": user.get("_imp_by")}
            )
        if tm.is_admin(user) and not tm.is_impersonating(user):
            payload["permission_catalog"] = tm.permission_catalog(AVAILABLE_STRATEGIES)
        if is_demo_mirror_user(user):
            payload["demo_mirror_admin"] = True
            payload["settings_readonly"] = True
            payload["can_link_telegram"] = False
            # Ensure strategy/finder/grid/history panels are visible for house trades.
            blocks = payload.get("allowed_blocks")
            if isinstance(blocks, list):
                merged = list(dict.fromkeys([*blocks, *DEMO_MIRROR_VIEW_BLOCKS]))
                payload["allowed_blocks"] = merged
            else:
                payload["allowed_blocks"] = list(DEMO_MIRROR_VIEW_BLOCKS)
        else:
            payload["demo_mirror_admin"] = False
            payload["settings_readonly"] = False
            payload["can_link_telegram"] = True
        return payload

    def _handle_bot_proxy(self, path: str, parsed) -> None:
        parts = [p for p in path.split("/") if p]
        # parts: bot-proxy, bot, ...
        if len(parts) < 2:
            self._json(400, {"error": "bot required"})
            return
        bot = parts[1]
        if not tm.user_may_use_bot(self.user, bot):
            self._json(403, {"error": "block not allowed", "bot": bot})
            return
        rest = "/".join(parts[2:])
        method = self.command.upper()
        # Demo mirrors admin trades — block mutations (start/stop/forceexit/…).
        if method not in ("GET", "HEAD") and not self._require_writable():
            return
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
        method = self.command.upper()
        # Shared stack: strategy forceexit must go through trade-executor (signal-engine has no fills).
        if method == "POST" and bot == "strategy" and rest.rstrip("/") == "forceexit":
            try:
                payload = json.loads(body.decode("utf-8") or "{}") if body else {}
            except json.JSONDecodeError:
                payload = {}
            trade_id = payload.get("tradeid") or payload.get("trade_id")
            uid = self._control_uid()
            try:
                import trade_executor as te

                out = te.force_close_trade(uid, int(trade_id))
                self._json(200, out)
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        rest_norm = rest.rstrip("/")
        is_status = method == "GET" and rest_norm == "status"
        is_light = rest_norm in ("show_config", "count", "ping", "version")
        # Heavy/wedged ctbot must not freeze the UI for 60s.
        if is_status:
            timeout = 2.0
        elif is_light:
            timeout = 3.0
        else:
            timeout = 60.0
        proxy_user = self.user or {}
        shared_strategy = (
            bot == "strategy"
            and _shared_strategy_uses_signal_engine()
            and not tm.is_admin(self.user)
        )
        if shared_strategy:
            # User strategy units are stopped: every tenant's strategy panel
            # is served by the shared signal-engine (admin ports/credentials).
            if method == "GET" and rest_norm == "balance":
                # The shared engine holds the admin exchange wallet. Never
                # expose it to tenants; the UI renders "—" for empty currencies.
                self._json(
                    200,
                    {
                        "currencies": [],
                        "shared_engine": True,
                        "note": "per-user balance is not exposed by the shared signal-engine",
                    },
                )
                return
            if method == "GET" and rest_norm in (
                "show_config",
                "profit",
                "whitelist",
                "ping",
                "version",
                "count",
            ):
                proxy_user = {"id": "admin", "role": "admin"}
                timeout = min(timeout, 8.0)
            elif method == "POST" and rest_norm == "reload_config":
                self._json(
                    403,
                    {
                        "error": "shared signal-engine config is managed by admin",
                        "shared_engine": True,
                    },
                )
                return
        status, headers, body_out = tm.proxy_bot_request(
            proxy_user,
            bot,
            self.command,
            rest,
            query=query or None,
            body_bytes=body or None,
            content_type=self.headers.get("Content-Type"),
            timeout=timeout,
        )
        if (
            shared_strategy
            and method == "GET"
            and rest_norm == "show_config"
            and status == 200
        ):
            # Engine stake belongs to the admin wallet; tenants keep their own
            # stake in user_settings and the UI must not overwrite it from here.
            try:
                shared_cfg = json.loads(body_out or b"{}")
                if isinstance(shared_cfg, dict) and "stake_amount" in shared_cfg:
                    shared_cfg.pop("stake_amount", None)
                    body_out = json.dumps(shared_cfg).encode("utf-8")
            except Exception:  # noqa: BLE001
                pass
        # Manual UI reload — optional Telegram notify (prefs default off).
        if (
            method == "POST"
            and rest.rstrip("/") == "reload_config"
            and status in (200, 201)
            and self.user
        ):
            try:
                import telegram_links as tg_links

                uid = (
                    "admin"
                    if tm.is_admin(self.user) and not tm.is_impersonating(self.user)
                    else str(self.user["id"])
                )
                tg_links.notify_user(
                    uid,
                    tg_links.format_bot_event_ru(
                        action="reload",
                        bot=bot,
                        user_id=uid,
                        detail="Конфиг перезагружен из панели",
                    ),
                    kind="bot_reload",
                )
            except Exception:
                pass
        if is_status and status == 200:
            try:
                data = json.loads(body_out or b"null")
                if isinstance(data, list):
                    remember_open_trades(bot, data, source="live")
                    _open_trades_live_backoff.pop(_open_trades_backoff_key(bot), None)
            except Exception:  # noqa: BLE001
                pass
        elif is_status and status in (401, 403, 502, 504, 503):
            _open_trades_live_backoff[_open_trades_backoff_key(bot)] = time.monotonic() + 30.0
            cached = get_cached_open_trades(bot, max_age=OPEN_TRADES_STALE_SEC)
            if cached is not None and cached.get("trades"):
                payload = cached["trades"]
                body_out = json.dumps(payload).encode("utf-8")
                status = 200
                headers = {
                    "content-type": "application/json",
                    "x-open-trades-cache": "1",
                    "x-open-trades-age": str(cached.get("age_sec") or 0),
                }
            else:
                db_trades = load_open_trades_from_db(bot)
                remember_open_trades(bot, db_trades, source="db")
                body_out = json.dumps(db_trades).encode("utf-8")
                status = 200
                headers = {
                    "content-type": "application/json",
                    "x-open-trades-cache": "db",
                }
        self.send_response(status)
        ct = headers.get("content-type") or "application/octet-stream"
        self.send_header("Content-Type", ct)
        if headers.get("x-open-trades-cache"):
            self.send_header("X-Open-Trades-Cache", headers["x-open-trades-cache"])
        if headers.get("x-open-trades-age"):
            self.send_header("X-Open-Trades-Age", headers["x-open-trades-age"])
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
            public = tm._public_user(user)
            self._json(200, {"token": token, "user": public, "role": public.get("role")})
            return

        # Telegram Mini App: login via validated WebApp initData for linked chat_id
        if method == "POST" and path == "/auth/telegram-webapp":
            import telegram_links as tg_links

            data = self._read_json()
            init_data = str(data.get("init_data") or data.get("initData") or "").strip()
            tg_user = tg_links.validate_webapp_init_data(init_data)
            if not tg_user or not tg_user.get("id"):
                self._json(401, {"error": "invalid telegram initData"})
                return
            chat_id = str(tg_user["id"])
            linked_uid = tg_links.user_for_chat(chat_id)
            if not linked_uid:
                self._json(
                    401,
                    {
                        "error": "telegram_not_linked",
                        "message": "Сначала войдите логином и привяжите Telegram в Настройках",
                    },
                )
                return
            link = tg_links.get_link(linked_uid)
            if not link.get("enabled") or str(link.get("chat_id") or "") != chat_id:
                self._json(401, {"error": "telegram_link_disabled"})
                return
            user = tm.get_user_by_id(linked_uid)
            if not user or not user.get("enabled", True):
                self._json(401, {"error": "user disabled"})
                return
            token = tm.issue_token(user)
            public = tm._public_user(user)
            self._json(
                200,
                {
                    "token": token,
                    "user": public,
                    "role": public.get("role"),
                    "auth_via": "telegram_webapp",
                },
            )
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
            elif method == "DELETE":
                self._handle_delete(path, parsed)
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

    def _sse_bots_events(self, parsed) -> None:
        """Long-lived SSE stream of control-plane events for the current user."""
        uid = self._control_uid()
        qs = parse_qs(parsed.query)
        try:
            after_id = int((qs.get("after") or ["0"])[0] or 0)
        except ValueError:
            after_id = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # A stalled client must not park this thread forever on wfile.write.
        try:
            self.connection.settimeout(60.0)
        except OSError:
            pass

        queue: list[dict[str, Any]] = []
        qlock = threading.Lock()

        def _on_event(ev: dict[str, Any]) -> None:
            if str(ev.get("user_id") or "") != uid:
                return
            with qlock:
                queue.append(ev)

        control_plane.add_listener(_on_event)
        try:
            # Initial snapshot
            snap = control_plane.snapshot_all(uid)
            init = {
                "id": after_id,
                "user_id": uid,
                "bot": None,
                "type": "bot.snapshot",
                "payload": snap,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            chunk = f"event: bot.snapshot\ndata: {json.dumps(init, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

            # Catch-up from DB
            for ev in control_plane.events_since(uid, after_id=after_id, limit=200):
                after_id = max(after_id, int(ev["id"]))
                line = f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

            last_ping = time.time()
            while True:
                batch: list[dict[str, Any]] = []
                with qlock:
                    if queue:
                        batch = list(queue)
                        queue.clear()
                for ev in batch:
                    after_id = max(after_id, int(ev.get("id") or 0))
                    line = f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    self.wfile.write(line.encode("utf-8"))
                if batch:
                    self.wfile.flush()
                now = time.time()
                if now - last_ping >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                time.sleep(0.35)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        finally:
            control_plane.remove_listener(_on_event)

    def _handle_get(self, path: str, parsed) -> None:
        if path == "/auth/me":
            self._json(200, self._auth_me_payload())
            return
        if path == "/bots":
            uid = self._control_uid()
            control_plane.ensure_init()
            self._json(200, control_plane.snapshot_all(uid))
            return
        if path == "/bots/events":
            self._sse_bots_events(parsed)
            return
        if path.startswith("/bots/"):
            bot = path.split("/")[2] if len(path.split("/")) >= 3 else ""
            bot = resolve_bot(bot)
            if bot not in CONFIGS:
                self._json(400, {"error": "invalid bot"})
                return
            if not tm.user_may_use_bot(self.user, bot):
                self._json(403, {"error": "block not allowed", "bot": bot})
                return
            self._json(200, control_plane.snapshot_bot(self._control_uid(), bot))
            return
        if path == "/permission-catalog":
            if not self._require_admin():
                return
            self._json(200, tm.permission_catalog(AVAILABLE_STRATEGIES))
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
        if path == "/trading-enabled":
            uid = self._control_uid()
            # Prefer control-plane desired when present
            flags = user_trading.load_trading_flags(uid)
            for b in ("strategy", "grid", "finder"):
                d = control_plane.get_desired(uid, b)
                if d is not None:
                    flags[b] = bool(d)
            self._json(200, {"user_id": uid, "flags": flags})
            return
        if path == "/llm":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            self._json(200, _llm_bot_view(self._control_uid()))
            return
        if path == "/llm50":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            self._json(200, _llm50_bot_view(self._control_uid()))
            return
        if path == "/cursor":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            self._json(200, _cursor_bot_view(self._control_uid()))
            return
        if path == "/cursor50":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            self._json(200, _cursor50_bot_view(self._control_uid()))
            return
        if path == "/telegram":
            import telegram_links as tg_links

            uid = (
                "admin"
                if tm.is_admin(self.user) and not tm.is_impersonating(self.user)
                else str(self.user["id"])
            )
            self._json(200, tg_links.get_link(uid))
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
            payload["test_settings"] = test_strategy_settings_payload()
            # Canonical limits block — UI must prefer this over any live bot field.
            payload["limits"] = bot_limits_public_payload()
            self._json(200, payload)
            return
        if path == "/strategies":
            with maybe_admin_trade_context() as mirrored:
                payload = get_strategies_payload()
                payload["demo_mirror_admin"] = bool(mirrored)
                payload["settings_readonly"] = bool(mirrored)
                self._json(200, payload)
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
        if path == "/stats-bundle":
            qs = parse_qs(parsed.query)
            force = str((qs.get("force") or [""])[0]).lower() in ("1", "true", "yes")
            try:
                self._json(200, get_stats_bundle(force=force))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/open-summary":
            try:
                bundle = get_stats_bundle(force=False)
                summary = bundle.get("open_summary") or build_open_trades_summary()
                summary = dict(summary)
                summary["cached"] = bool(bundle.get("cached"))
                self._json(200, summary)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/open-trades":
            qs = parse_qs(parsed.query)
            refresh = str((qs.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            try:
                self._json(200, get_open_trades_bundle(refresh=refresh))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
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
                self._json(200, get_strategy_rating_payload(period, limit, user_q, (qs.get("tf") or ["5m"])[0], (qs.get("bot") or ["all"])[0], (qs.get("side") or ["all"])[0]))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/strategy-pair-rating":
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
                self._json(200, get_strategy_pair_rating_payload(period, limit, user_q, (qs.get("tf") or ["5m"])[0], (qs.get("bot") or ["all"])[0], (qs.get("side") or ["all"])[0]))
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
                self._json(200, get_pair_rating_payload(period, limit, user_q, (qs.get("tf") or ["5m"])[0], (qs.get("bot") or ["all"])[0], (qs.get("side") or ["all"])[0]))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/strategy-rating-daily":
            qs = parse_qs(parsed.query)
            period = (qs.get("period") or ["all"])[0]
            user_q = (qs.get("user") or qs.get("user_id") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["60"])[0])
            except ValueError:
                limit = 60
            if _normalize_rating_user_filter(user_q) and not tm.is_admin(self.user):
                self._json(403, {"error": "admin required"})
                return
            try:
                self._json(200, get_strategy_daily_rating_payload(period, limit, user_q, (qs.get("tf") or ["5m"])[0], (qs.get("bot") or ["all"])[0], (qs.get("side") or ["all"])[0]))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/strategy-rating-weekday-matrix":
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
                self._json(200, get_strategy_weekday_matrix_payload(period, limit, user_q, (qs.get("tf") or ["5m"])[0], (qs.get("bot") or ["all"])[0], (qs.get("side") or ["all"])[0]))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/adaptive-scan":
            self._json(200, get_adaptive_scan_status(BASE))
            return
        if path == "/adaptive-scan/settings":
            self._json(200, get_adaptive_scan_status(BASE))
            return
        if path == "/position-reconcile":
            try:
                payload = reconcile_positions(BASE)
                payload["auto_sync"] = {
                    "interval_sec": int(POSITION_RECONCILE_INTERVAL_SEC),
                    "last": dict(_position_reconcile_last),
                }
                self._json(200, payload)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc), "ok": False})
            return
        if path.startswith("/bybit-grid"):
            if not self._require_block("bybitgrid"):
                return
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
        if path == "/llm":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            if not self._require_writable():
                return
            try:
                self._json(200, _llm_bot_update(self._control_uid(), data if isinstance(data, dict) else {}))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/llm50":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            if not self._require_writable():
                return
            try:
                self._json(200, _llm50_bot_update(self._control_uid(), data if isinstance(data, dict) else {}))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/cursor":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            if not self._require_writable():
                return
            try:
                self._json(200, _cursor_bot_update(self._control_uid(), data if isinstance(data, dict) else {}))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/cursor50":
            if not tm.user_may_use_block(self.user, "llm"):
                self._json(403, {"error": "block not allowed"})
                return
            if not self._require_writable():
                return
            try:
                self._json(200, _cursor50_bot_update(self._control_uid(), data if isinstance(data, dict) else {}))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/strategy-bot/restart":
            if not self._require_admin():
                return
            self._restart_strategy_bot()
            return
        if path == "/secrets/activate":
            profile_id = str(data.get("profile_id") or data.get("id") or "").strip()
            if not profile_id:
                self._json(400, {"error": "profile_id required"})
                return
            target_id = self._secrets_owner_id(data)
            if not (tm.is_admin(self.user) and not tm.is_impersonating(self.user)):
                if data.get("user_id") and str(data.get("user_id")) != str(self.user["id"]):
                    self._json(403, {"error": "cannot activate secrets for another user"})
                    return
                target_id = str(self.user["id"])
            try:
                result = tm.activate_key_profile(target_id, profile_id)
                try:
                    import trade_executor as te

                    te.clear_exchange_cache(None if target_id == "admin" else target_id)
                except Exception:
                    pass
                self._json(200, result)
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/auth/impersonate":
            if not self._require_admin():
                return
            try:
                token, target = tm.issue_impersonation_token(
                    self.user, str(data.get("user_id") or "")
                )
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
                return
            except (ValueError, PermissionError) as exc:
                self._json(400 if isinstance(exc, ValueError) else 403, {"error": str(exc)})
                return
            self._json(
                200,
                {
                    "token": token,
                    "user": target,
                    "role": target.get("role"),
                    "impersonating": True,
                    "impersonated_by": {
                        "id": self.user.get("id"),
                        "username": self.user.get("username"),
                    },
                },
            )
            return
        if path == "/auth/stop-impersonate":
            try:
                token, admin = tm.stop_impersonation_token(self.user or {})
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
                return
            self._json(
                200,
                {
                    "token": token,
                    "user": admin,
                    "role": admin.get("role"),
                    "impersonating": False,
                },
            )
            return
        if path == "/auth/switch-account":
            if not self.user:
                self._unauthorized()
                return
            try:
                token, target = tm.issue_switch_account_token(
                    self.user, str(data.get("target") or "")
                )
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
                return
            except (ValueError, PermissionError) as exc:
                self._json(
                    400 if isinstance(exc, ValueError) else 403,
                    {"error": str(exc)},
                )
                return
            # Full session payload so UI can paint Live/Demo immediately
            # (allowed_blocks / secrets) without waiting on a second /auth/me.
            dest_user = tm.get_user_by_id(str(target["id"])) or target
            if self.user.get("_imp_by"):
                dest_user = {**dest_user, "_imp_by": self.user.get("_imp_by")}
            # Temporarily bind request user so _auth_me_payload sees the target.
            prev_user = self.user
            self.user = dest_user
            try:
                payload = self._auth_me_payload()
            finally:
                self.user = prev_user
            payload["token"] = token
            self._json(200, payload)
            return
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
        if path == "/users/backfill-demo-pairs":
            if not self._require_admin():
                return
            self._json(200, tm.backfill_demo_pairs())
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
            if not self._require_admin():
                return
            # Default: DB policy (apply_whitelist=false). Explicit {"apply": true} only.
            apply_wl: bool | None = None
            if isinstance(data, dict) and "apply" in data:
                apply_wl = bool(data.get("apply"))
            try:
                self._json(200, trigger_strategy_scan(apply_whitelist=apply_wl))
            except ValueError as exc:
                self._json(409, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/position-reconcile/fix":
            try:
                # Same path as the 30m auto job (demo/live Bybit + archive ghosts).
                self._json(200, run_position_reconcile_auto(force=True))
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
        if path == "/force-close":
            if not self._require_writable():
                return
            bot = resolve_bot(data.get("bot") or "strategy")
            trade_id = data.get("trade_id") or data.get("tradeid")
            if trade_id is None:
                self._json(400, {"error": "trade_id required"})
                return
            if bot != "strategy":
                self._json(400, {"error": "force-close is for strategy shared stack; use bot forceexit"})
                return
            uid = self._control_uid()
            try:
                import trade_executor as te

                self._json(200, te.force_close_trade(uid, int(trade_id)))
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc), "ok": False})
            return
        if path.startswith("/bybit-grid"):
            if not self._require_block("bybitgrid"):
                return
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
        # Default: /pairs mutations (toggle, promote, limits, …)
        if not self._require_writable():
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
                group = data.get("group")
                self._json(
                    200,
                    set_all_strategies_enabled(
                        bool(data["enabled"]),
                        group=str(group) if group is not None else None,
                    ),
                )
            elif action == "promote_strategy_to_main":
                if not self._can_manage_strategy_placement():
                    return
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                self._json(200, promote_strategy_to_main(str(strategy_id)))
            elif action == "demote_strategy_to_test":
                if not self._can_manage_strategy_placement():
                    return
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                self._json(200, demote_strategy_to_test(str(strategy_id)))
            elif action == "move_all_strategies_to_test":
                if not self._can_manage_strategy_placement():
                    return
                self._json(200, move_all_strategies_to_test())
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
            elif action == "set_htf_stake":
                raw = data.get("stake_amount", data.get("value"))
                if raw is None:
                    self._json(400, {"error": "stake_amount required"})
                    return
                try:
                    self._json(200, set_htf_stake(float(raw)))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "set_strategy_sltp":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "sl" not in data or "tp" not in data:
                    self._json(400, {"error": "sl and tp required"})
                    return
                try:
                    self._json(
                        200,
                        set_strategy_sltp(str(strategy_id), data.get("sl"), data.get("tp")),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "set_strategy_entry_side":
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                if "entry_side" not in data:
                    self._json(400, {"error": "entry_side required"})
                    return
                try:
                    self._json(
                        200,
                        set_strategy_entry_side(str(strategy_id), str(data.get("entry_side") or "")),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
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
                group = data.get("group")
                try:
                    if reset:
                        self._json(
                            200,
                            set_all_strategies_ml_confidence(
                                reset=True,
                                group=str(group) if group is not None else None,
                            ),
                        )
                    else:
                        if "ml_confidence" not in data:
                            self._json(400, {"error": "ml_confidence required (or reset=true)"})
                            return
                        self._json(
                            200,
                            set_all_strategies_ml_confidence(
                                float(data["ml_confidence"]),
                                group=str(group) if group is not None else None,
                            ),
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
            elif action == "set_test_strategy_settings":
                try:
                    self._json(200, set_test_strategy_settings(data))
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
            elif action == "set_strategy_pairs":
                if "strategy" not in data:
                    self._json(400, {"error": "strategy required"})
                    return
                try:
                    self._json(
                        200,
                        set_strategy_pairs(
                            str(data["strategy"]),
                            data.get("pairs") if isinstance(data.get("pairs"), list) else [],
                        ),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "add_strategy_pair":
                if "strategy" not in data or "pair" not in data:
                    self._json(400, {"error": "strategy and pair required"})
                    return
                try:
                    self._json(
                        200,
                        add_strategy_pair(str(data["strategy"]), str(data["pair"])),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "remove_strategy_pair":
                if "strategy" not in data or "pair" not in data:
                    self._json(400, {"error": "strategy and pair required"})
                    return
                try:
                    self._json(
                        200,
                        remove_strategy_pair(str(data["strategy"]), str(data["pair"])),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif action == "seed_strategy_pairs":
                try:
                    self._json(
                        200,
                        seed_strategy_pairs_from_perf(
                            only_empty=bool(data.get("only_empty", False)),
                        ),
                    )
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
                            "toggle_strategy_trained_risk, set_strategy_entry_side, "
                            "set_strategy_ml_confidence, "
                            "set_all_strategies_ml_confidence, "
                            "set_strategy_pairs, add_strategy_pair, remove_strategy_pair, "
                            "seed_strategy_pairs, "
                            "set_dual_hedge, set_max_open_trades_per_strategy, "
                            "set_test_strategy_settings, or set_strategy_risk"
                        )
                    },
                )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            self._json(502, {"error": exc.read().decode()[:500]})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})

    def _secrets_owner_id(self, data: dict[str, Any] | None = None, qs=None) -> str:
        data = data or {}
        qs = qs or {}
        if tm.is_admin(self.user) and not tm.is_impersonating(self.user):
            return str(data.get("user_id") or (qs.get("user_id") or [None])[0] or "admin")
        return str(self.user["id"])

    def _handle_put(self, path: str, parsed) -> None:
        if path != "/secrets":
            self._json(404, {"error": "not found"})
            return
        data = self._read_json()
        qs = parse_qs(parsed.query)
        key = str(data.get("bybit_api_key") or "")
        secret = str(data.get("bybit_api_secret") or "")
        name = str(data.get("name") or data.get("label") or "").strip() or None
        demo_raw = data.get("bybit_demo_trading", data.get("demo_trading", False))
        if isinstance(demo_raw, bool):
            demo_trading = demo_raw
        else:
            demo_trading = str(demo_raw or "").strip().lower() in (
                "1",
                "true",
                "t",
                "yes",
                "y",
                "on",
            )
        target_id = self._secrets_owner_id(data, qs)
        if tm.is_admin(self.user) and not tm.is_impersonating(self.user):
            if target_id not in ("", "admin") and target_id != "admin":
                # Admin writing another user's secrets
                pass
            elif target_id in ("", "admin"):
                try:
                    result = tm.save_admin_secrets(
                        key, secret, demo_trading=demo_trading, name=name
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                try:
                    import trade_executor as te

                    te.clear_exchange_cache(None)
                except Exception:
                    pass
                self._json(200, result)
                return
        else:
            if data.get("user_id") and str(data.get("user_id")) != str(self.user["id"]):
                self._json(403, {"error": "cannot write secrets for another user"})
                return
            target_id = str(self.user["id"])
        try:
            result = tm.save_user_secrets(
                target_id, key, secret, demo_trading=demo_trading, name=name
            )
            try:
                import trade_executor as te

                te.clear_exchange_cache(target_id)
            except Exception:
                pass
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)

    def _handle_delete(self, path: str, parsed) -> None:
        if path == "/secrets":
            data = self._read_json() if int(self.headers.get("Content-Length") or 0) else {}
            qs = parse_qs(parsed.query)
            profile_id = str(
                data.get("profile_id")
                or data.get("id")
                or (qs.get("profile_id") or qs.get("id") or [None])[0]
                or ""
            ).strip()
            if not profile_id:
                self._json(400, {"error": "profile_id required"})
                return
            target_id = self._secrets_owner_id(data, qs)
            if not (tm.is_admin(self.user) and not tm.is_impersonating(self.user)):
                if data.get("user_id") and str(data.get("user_id")) != str(self.user["id"]):
                    self._json(403, {"error": "cannot delete secrets for another user"})
                    return
                target_id = str(self.user["id"])
            try:
                result = tm.delete_key_profile(target_id, profile_id)
                try:
                    import trade_executor as te

                    te.clear_exchange_cache(None if target_id == "admin" else target_id)
                except Exception:
                    pass
                self._json(200, result)
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def _handle_patch(self, path: str, parsed) -> None:
        if path == "/adaptive-scan" or path == "/adaptive-scan/settings":
            if not self._require_admin():
                return
            data = self._read_json()
            try:
                who = str((self.user or {}).get("username") or "admin")
                settings = set_adaptive_scan_settings(
                    BASE,
                    data if isinstance(data, dict) else {},
                    updated_by=who,
                )
                status = get_adaptive_scan_status(BASE)
                status["settings"] = settings
                status["stored_in"] = "control_plane.sqlite"
                self._json(200, status)
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path.startswith("/bots/"):
            if not self._require_writable():
                return
            parts = [p for p in path.split("/") if p]
            # bots, {bot}
            if len(parts) < 2:
                self._json(400, {"error": "bot required"})
                return
            bot = resolve_bot(parts[1])
            if bot not in ("finder", "strategy", "grid"):
                self._json(400, {"error": "invalid bot"})
                return
            if not tm.user_may_use_bot(self.user, bot):
                self._json(403, {"error": "block not allowed", "bot": bot})
                return
            data = self._read_json()
            if "enabled" not in data:
                self._json(400, {"error": "enabled bool required"})
                return
            enabled = bool(data.get("enabled"))
            if bot in ("finder", "grid") and enabled:
                self._json(400, {"error": "бот снят с панели"})
                return
            if enabled and bot_trading_disabled(bot):
                self._json(
                    400,
                    {
                        "error": "max_open_trades is 0 — set at least 1 active slot to start trading",
                        "trading_disabled": True,
                    },
                )
                return
            uid = self._control_uid()
            control_plane.ensure_init()
            snap = control_plane.set_desired(
                uid,
                bot,
                enabled,
                updated_by=str((self.user or {}).get("username") or uid),
                enqueue=True,
            )
            # Keep JSON flags in sync immediately for executor readers
            user_trading.save_trading_flags(uid, {bot: enabled})
            self._json(200, snap)
            return
        if path == "/trading-enabled":
            if not self._require_writable():
                return
            uid = self._control_uid()
            data = self._read_json()
            flags = data.get("flags") if isinstance(data.get("flags"), dict) else data
            if not isinstance(flags, dict):
                self._json(400, {"error": "flags object required"})
                return
            control_plane.ensure_init()
            saved_flags: dict[str, bool] = {}
            snaps = {}
            for key in ("strategy", "grid", "finder"):
                if key not in flags:
                    continue
                enabled = bool(flags[key])
                snaps[key] = control_plane.set_desired(
                    uid,
                    key,
                    enabled,
                    updated_by=str((self.user or {}).get("username") or uid),
                    enqueue=True,
                )
                saved_flags[key] = enabled
            # Persist full merged flags file
            saved = user_trading.save_trading_flags(uid, flags)
            self._json(200, {"user_id": uid, "flags": saved, "bots": snaps})
            return
        if path == "/telegram":
            if not self._require_writable():
                return
            import telegram_links as tg_links

            uid = (
                "admin"
                if tm.is_admin(self.user) and not tm.is_impersonating(self.user)
                else str(self.user["id"])
            )
            data = self._read_json()
            try:
                chat_id = str(data.get("chat_id") if "chat_id" in data else "").strip()
                enabled = bool(data.get("enabled", True)) if chat_id else False
                notify = data.get("notify") if isinstance(data.get("notify"), dict) else None
                # Optional Mini App auto-link: validate initData and take user.id
                init_data = str(data.get("init_data") or data.get("initData") or "").strip()
                if init_data and not chat_id:
                    tg_user = tg_links.validate_webapp_init_data(init_data)
                    if not tg_user or not tg_user.get("id"):
                        self._json(400, {"error": "невалидные данные Telegram WebApp"})
                        return
                    chat_id = str(tg_user["id"])
                saved = tg_links.set_link(uid, chat_id, enabled=enabled, notify=notify)
                if chat_id:
                    try:
                        on_kinds = [
                            k["label"]
                            for k in (saved.get("notify_kinds") or [])
                            if (saved.get("notify") or {}).get(k["id"])
                        ]
                        kinds_txt = ", ".join(on_kinds[:6]) if on_kinds else "нет выбранных типов"
                        tg_links.send_message(
                            chat_id,
                            f"✅ CryptoTools: чат привязан к аккаунту «{uid}».\n"
                            f"Уведомления: {kinds_txt}.",
                        )
                    except Exception:
                        pass
                self._json(200, saved)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "users":
            self._json(404, {"error": "not found"})
            return
        if not self._require_admin():
            return
        data = self._read_json()
        try:
            updated = tm.patch_user(
                parts[2],
                data,
                valid_strategy_ids=_strategy_ids(),
            )
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        # If strategies were restricted, force-disable the rest in tenant config.
        if "allowed_strategies" in data and updated.get("id"):
            try:
                _clamp_tenant_enabled_strategies(
                    str(updated["id"]), updated.get("allowed_strategies")
                )
            except Exception:
                pass
        self._json(200, {"user": updated})


def _clamp_tenant_enabled_strategies(
    user_id: str, allowed: list[str] | None
) -> None:
    """When allowlist is set, turn off strategies outside it in the tenant file."""
    if allowed is None:
        return
    allow = set(allowed)
    path = tm.tenant_user_data(user_id) / "enabled_strategies.json"
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(raw, dict):
        return
    enabled = raw.get("enabled")
    if not isinstance(enabled, dict):
        return
    changed = False
    for sid, on in list(enabled.items()):
        if on and sid not in allow:
            enabled[sid] = False
            changed = True
    if not changed:
        return
    raw["enabled"] = enabled
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    apply_tenant_bot_limits()
    apply_strategies_prefs()
    ensure_router_config()
    _setup_server_logging()
    try:
        heal = ensure_strategy_whitelist_from_db()
        _server_log.info("strategy whitelist DB heal: %s", heal)
    except Exception:
        _server_log.exception("strategy whitelist DB heal failed")
    host = os.environ.get("PAIR_CONFIG_HOST", "127.0.0.1")
    port = int(os.environ.get("PAIR_CONFIG_PORT", "8090"))
    _server_log.info("pair-config server starting on %s:%s", host, port)
    start_adaptive_scan_scheduler(
        BASE,
        ranging_scan=trigger_ranging_scan,
        # Adaptive strategy scan is report-only — never shrink StaticPairList.
        strategy_scan=lambda: trigger_strategy_scan(apply_whitelist=False),
        poll_sec=60,
    )
    start_stats_cache_scheduler()
    start_open_trades_cache_scheduler()
    start_position_reconcile_scheduler()
    try:
        control_plane.init_db()
        control_plane.seed_from_trading_flags()
        bot_reconcile.start_reconcile_worker(poll_sec=2.5)
        _server_log.info("control-plane + bot reconcile worker ready")
    except Exception:
        _server_log.exception("control-plane init failed")
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
    """Mirror DB bot_limits → config files (never overwrite DB from stale configs)."""
    env_file = Path(os.environ.get("CT_ENV", "/home/cryptotools/.cryptotools.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    limits = ensure_bot_limits_snapshot()
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
