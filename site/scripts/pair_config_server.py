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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
import user_trading
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
from adaptive_scan_scheduler import get_adaptive_scan_status, start_adaptive_scan_scheduler
from reconcile_positions import (
    archive_stale_trade,
    fix_reconcile,
    reconcile as reconcile_positions,
)

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
FINDER_BOT_CFG = BASE / "user_data" / "finder_bot.json"
APP_TZ = timezone(timedelta(hours=3))  # UTC+3 (Moscow, no DST)

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
        fh = logging.FileHandler(log_file, encoding="utf-8")
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
        "desc": "ML pack · sim new_psar · test ML PnL 87.1 USDT · SL -2.0% · TP 2.2% · gate>=45% · exp exp14_lgbm_gate045",
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


# Birth group from catalog (static). Runtime panel may differ via strategy_ui_placement.json.
CATALOG_TEST_IDS = frozenset(
    s["id"] for s in AVAILABLE_STRATEGIES if s.get("test_group")
)
# Backward-compat alias; prefer effective_ui_panel() / is_effective_test_strategy().
TEST_STRATEGY_IDS = CATALOG_TEST_IDS

_PLACEMENT_NOTE = (
    "main = Strategies panel; catalog test_group and not in main = Тестовые; "
    "hidden = neither (former live pack kept for history labels / sim)."
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
    # New catalog ids: non-test → hidden; test stay out of both (panel=test).
    catalog_ids = {s["id"] for s in AVAILABLE_STRATEGIES}
    known = seen_main | seen_hidden
    for sid in catalog_ids:
        if sid in known:
            continue
        if _catalog_birth_test(sid):
            continue
        hidden.append(sid)
        seen_hidden.add(sid)
    out = {
        "main": main,
        "hidden": hidden,
        "_note": str(raw.get("_note") or _PLACEMENT_NOTE),
    }
    if "_hidden_disabled" in raw:
        out["_hidden_disabled"] = bool(raw.get("_hidden_disabled"))
    return out


def save_strategy_ui_placement(data: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_placement(data)
    if "_hidden_disabled" in data:
        normalized["_hidden_disabled"] = bool(data.get("_hidden_disabled"))
    path = strategy_ui_placement_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(normalized, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)
    return normalized


def ensure_strategy_ui_placement() -> dict[str, Any]:
    """Load placement; create defaults (empty main, hide former live) once."""
    path = strategy_ui_placement_file()
    existed = path.is_file()
    raw: dict[str, Any] | None = None
    if existed:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
    if not existed or raw is None:
        return save_strategy_ui_placement(
            {
                "main": [],
                "hidden": _default_hidden_ids(),
                "_note": _PLACEMENT_NOTE,
                "_hidden_disabled": False,
            }
        )
    normalized = _normalize_placement(raw)
    normalized["_hidden_disabled"] = bool(raw.get("_hidden_disabled"))
    # Persist if merge added new hidden ids.
    if set(normalized.get("main") or []) != set(raw.get("main") or []) or set(
        normalized.get("hidden") or []
    ) != set(raw.get("hidden") or []):
        normalized["_hidden_disabled"] = bool(raw.get("_hidden_disabled"))
        return save_strategy_ui_placement(normalized)
    return normalized


def effective_ui_panel(sid: str, placement: dict[str, Any] | None = None) -> str:
    """Return 'main' | 'test' | 'hidden' for a strategy id."""
    p = placement if placement is not None else ensure_strategy_ui_placement()
    main = set(p.get("main") or [])
    hidden = set(p.get("hidden") or [])
    if sid in main:
        return "main"
    if sid in hidden:
        return "hidden"
    if _catalog_birth_test(sid):
        return "test"
    return "hidden"


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
    return {
        "main": main_ids,
        "test": test_ids,
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
          AND cd_key IN ('ml_confidence', 'ml_gate_confidence', 'ml_predicted', 'ml_min_confidence')
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


def closed_profit_from_db(bot: str) -> dict[str, Any]:
    """Sum closed PnL from sqlite (works when ctbot /profit is empty or shared-stack)."""
    bot = resolve_bot(bot)
    empty = {
        "profit_closed_coin": 0.0,
        "profit_closed_fiat": 0.0,
        "trade_count": 0,
        "source": "db",
    }
    if bot not in CONFIGS:
        return empty
    db_path = _bot_db_path(bot)
    if not db_path:
        return empty
    query = """
        SELECT
            COUNT(*) AS n,
            COALESCE(SUM(COALESCE(close_profit_abs, realized_profit, 0)), 0) AS pnl
        FROM trades
        WHERE is_open = 0 AND close_date IS NOT NULL
    """
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(query).fetchone()
        n = int(row[0] or 0) if row else 0
        pnl = float(row[1] or 0.0) if row else 0.0
        return {
            "profit_closed_coin": round(pnl, 6),
            "profit_closed_fiat": round(pnl, 6),
            "trade_count": n,
            "source": "db",
        }
    except sqlite3.Error as exc:
        _server_log.warning("closed profit %s: %s", bot, exc)
        return empty


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


def load_open_trades_from_db(bot: str) -> list[dict[str, Any]]:
    """Open trades from sqlite (stake only; PnL may be missing without live mark)."""
    bot = resolve_bot(bot)
    if bot not in CONFIGS:
        return []
    db_path = _bot_db_path(bot)
    if not db_path:
        return []
    query = """
        SELECT id, pair, strategy, enter_tag, is_short,
               open_date, open_rate, stake_amount, amount, leverage,
               close_profit, close_profit_abs, realized_profit
        FROM trades
        WHERE is_open = 1
        ORDER BY id DESC
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            stake = float(r["stake_amount"] or 0)
            ratio = r["close_profit"]
            abs_pnl = r["close_profit_abs"]
            if abs_pnl is None and ratio is not None:
                try:
                    abs_pnl = float(ratio) * stake
                except (TypeError, ValueError):
                    abs_pnl = 0.0
            profit_abs = float(abs_pnl or 0)
            profit_pct = float(ratio or 0) * 100.0 if ratio is not None else None
            if ratio is None and r["open_rate"]:
                try:
                    from user_exchange import fetch_public_klines, ft_pair_to_symbol

                    sym = ft_pair_to_symbol(str(r["pair"]))
                    tick_rows = fetch_public_klines(sym, interval="1", limit=1)
                    if tick_rows:
                        mark = float(tick_rows[-1][4])
                        open_rate = float(r["open_rate"])
                        lev = float(r["leverage"] or 1) or 1.0
                        is_short = bool(r["is_short"])
                        if is_short:
                            pr = ((open_rate - mark) / open_rate) * lev
                        else:
                            pr = ((mark - open_rate) / open_rate) * lev
                        profit_abs = pr * stake
                        profit_pct = pr * 100.0
                except Exception:
                    pass
            out.append(
                {
                    "bot": bot,
                    "trade_id": int(r["id"]),
                    "pair": r["pair"],
                    "strategy": r["strategy"],
                    "enter_tag": r["enter_tag"],
                    "is_short": bool(r["is_short"]),
                    "open_date": r["open_date"],
                    "open_rate": r["open_rate"],
                    "amount": r["amount"],
                    "leverage": r["leverage"],
                    "stake_amount": stake,
                    "profit_abs": profit_abs,
                    "profit_pct": profit_pct,
                    "total_profit_abs": profit_abs,
                    "total_profit_ratio": (profit_pct / 100.0) if profit_pct is not None else None,
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
        if live is not None:
            # _fetch already remembered
            cached = get_cached_open_trades(bot, max_age=None, tenant_key=key) or {}
            out[bot] = {
                "trades": live,
                "age_sec": 0.0,
                "source": "live",
                "ts": cached.get("ts") or time.time(),
            }
            continue
        cached = get_cached_open_trades(bot, max_age=None, tenant_key=key)
        cached_trades = list((cached or {}).get("trades") or [])
        if cached is not None and cached_trades:
            out[bot] = {**cached, "source": f"cache:{cached.get('source') or 'live'}"}
            continue
        db_trades = load_open_trades_from_db(bot)
        remember_open_trades(bot, db_trades, source="db", tenant_key=key)
        out[bot] = {
            "trades": db_trades,
            "age_sec": 0.0,
            "source": "db",
            "ts": time.time(),
        }
    return out


def get_open_trades_bundle(*, refresh: bool = False) -> dict[str, Any]:
    """Warm payload of open trades per bot for the current tenant.

    Request path is never blocked on ctbot: cache → sqlite only.
    Live refresh happens in the background warmer (or refresh=1).
    """
    key = _stats_tenant_key()
    if refresh:
        # Still prefer not to stall the HTTP worker: kick a background refresh
        # and return whatever we already have (cache/db).
        threading.Thread(
            target=lambda: refresh_open_trades_cache_for_bots(tenant_key=key),
            name=f"open-trades-refresh-{key}",
            daemon=True,
        ).start()

    bots: dict[str, Any] = {}
    for bot in CONFIGS:
        cached = get_cached_open_trades(bot, max_age=None, tenant_key=key)
        cached_trades = list((cached or {}).get("trades") or [])
        if cached is not None and cached_trades:
            entry = dict(cached)
        else:
            db_trades = load_open_trades_from_db(bot)
            if db_trades or cached is None:
                remember_open_trades(bot, db_trades, source="db", tenant_key=key)
                entry = {
                    "trades": db_trades,
                    "age_sec": 0.0,
                    "source": "db",
                    "ts": time.time(),
                }
            else:
                entry = dict(cached)
        entry["closed_profit"] = closed_profit_from_db(bot)
        bots[bot] = entry
    return {
        "bots": bots,
        "built_at": int(time.time() * 1000),
        "cached": True,
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
    Strategy-bot slot ceiling for UI/executor.
    Test-block max_open_trades is a real concurrent limit and must not be
    silently capped by a lower bot_limits.strategy value.
    """
    try:
        strat = max(0, int(load_bot_limits().get("strategy", 0)))
    except Exception:  # noqa: BLE001
        strat = 0
    try:
        test_max = max(0, int(load_test_strategy_settings().get("max_open_trades") or 0))
    except Exception:  # noqa: BLE001
        test_max = 0
    return max(strat, test_max)


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
        if live is None:
            cached = get_cached_open_trades(bot, max_age=OPEN_TRADES_STALE_SEC)
            if cached is not None:
                sources[bot] = f"cache:{cached.get('source') or 'live'}"
                for t in cached["trades"]:
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
                continue
            sources[bot] = "db"
            trades.extend(load_open_trades_from_db(bot))
            continue
        sources[bot] = "live"
        for t in live:
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
    if tm.is_admin(u) and not tm.is_impersonating(u):
        return "admin"
    uid = str(u.get("id") or "anon")
    return f"user:{uid}"


def build_stats_bundle(limit: int = 0) -> dict[str, Any]:
    """Assemble closed trades + Bybit history for the current tenant context."""
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
                raw = INF_MAX_OPEN_TRADES
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
    """Reload until live show_config matches the value written to disk.

    ``/reload_config`` is async (returns immediately, worker applies later), so we
    poll ``show_config`` instead of a single short sleep.
    Fail fast only when the bot API is completely unreachable *before* any reload.
    Mid-reload flaps must not abort — POST can fail while the worker still applies.
    """
    bot = resolve_bot(bot)
    expected = int(expected)
    live = _live_max_open_trades(bot, timeout=2.0)
    if live == expected:
        return {"live_applied": True, "live_max_open_trades": live, "reloads": 0}
    if live is None:
        return {
            "live_applied": False,
            "live_max_open_trades": None,
            "reloads": 0,
            "reload_warning": (
                "бот недоступен — лимит записан на диск и применится после запуска"
            ),
        }

    last_err: str | None = None
    reloads = 0
    saw_live = True

    def _fire_reload() -> bool:
        nonlocal reloads, last_err
        try:
            api_call(
                RELOAD[bot],
                "POST",
                retries=1,
                timeout=min(LIMIT_RELOAD_TIMEOUT, 8.0),
            )
            reloads += 1
            return True
        except BaseException as exc:  # noqa: BLE001
            last_err = str(exc)
            return False

    def _poll_until(timeout_sec: float) -> bool:
        nonlocal live, saw_live
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(0.6)
            cur = _live_max_open_trades(bot, timeout=2.0)
            if cur is None:
                # Bot often flaps during RELOAD_CONFIG — keep waiting.
                continue
            saw_live = True
            live = cur
            if live == expected:
                return True
        return False

    # Pass 1: reload + wait for async apply
    _fire_reload()
    if _poll_until(20.0):
        return {"live_applied": True, "live_max_open_trades": live, "reloads": reloads}

    # Pass 2: another reload (first may have raced with a concurrent write)
    _fire_reload()
    if _poll_until(15.0):
        return {"live_applied": True, "live_max_open_trades": live, "reloads": reloads}

    # Final read — value may have landed just after the last poll window
    cur = _live_max_open_trades(bot, timeout=3.0)
    if cur is not None:
        saw_live = True
        live = cur
        if live == expected:
            return {"live_applied": True, "live_max_open_trades": live, "reloads": reloads}

    if not saw_live or live is None:
        return {
            "live_applied": False,
            "live_max_open_trades": None,
            "reloads": reloads,
            "reload_warning": (
                "бот недоступен — лимит записан на диск и применится после запуска"
            ),
        }

    msg = (
        last_err
        or f"бот ещё показывает max_open_trades={live}, на диске {expected}"
    )
    return {
        "live_applied": False,
        "live_max_open_trades": live,
        "reloads": reloads,
        "reload_warning": msg,
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
            max_trades = INF_MAX_OPEN_TRADES
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


def get_strategy_catalog() -> list[dict]:
    """Catalog for UI. Sorted by num ascending; strategies without num go last.

    Effective test_group / ui_panel come from strategy_ui_placement.json so promote/demote
    move rows between Тестовые and Стратегии without deleting catalog entries.
    """
    apply_hidden_disable_once()
    placement = ensure_strategy_ui_placement()
    rows: list[dict] = []
    for s in AVAILABLE_STRATEGIES:
        row = dict(s)
        panel = effective_ui_panel(s["id"], placement)
        row["ui_panel"] = panel
        row["catalog_test_group"] = bool(s.get("test_group"))
        # UI uses test_group for catalogByGroup — effective panel wins.
        row["test_group"] = panel == "test"
        rows.append(row)
    user = tc.current_user()
    allow = tm.user_allowed_strategies(user) if user else None
    if allow is not None:
        allow_set = set(allow)
        rows = [s for s in rows if s["id"] in allow_set]

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
    user = tc.current_user()
    if user and not tm.user_may_use_strategy(user, strategy_id):
        raise PermissionError(f"strategy not allowed: {strategy_id}")
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


def _object_map(value: Any) -> dict[str, Any]:
    """enabled_strategies.json fields must be objects; a bool/list here used to crash /strategies."""
    return value if isinstance(value, dict) else {}


def load_enabled_map() -> dict[str, bool]:
    result = default_enabled_map()
    if not enabled_strategies_file().is_file():
        return result
    try:
        data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    if not isinstance(data, dict):
        return result
    enabled = _object_map(data.get("enabled"))
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
    inverted = _object_map(data.get("inverted") if isinstance(data, dict) else None)
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
    if not enabled_strategies_file().is_file():
        return result
    try:
        data = json.loads(enabled_strategies_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    stored = _object_map(data.get("ml_confidence") if isinstance(data, dict) else None)
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
    stored = _object_map(data.get("trained_risk") if isinstance(data, dict) else None)
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
        prev = _object_map(data.get("inverted"))
        data["inverted"] = {sid: bool(prev.get(sid, False)) for sid in enabled}
    if trained_risk is not None:
        data["trained_risk"] = {sid: bool(trained_risk.get(sid, False)) for sid in enabled}
    else:
        prev_tr = _object_map(data.get("trained_risk"))
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
    """Admin: move birth-test strategy from Тестовые → Стратегии (enabled=false)."""
    apply_hidden_disable_once()
    strategy_id = validate_strategy(strategy_id)
    placement = ensure_strategy_ui_placement()
    panel = effective_ui_panel(strategy_id, placement)
    if panel != "test":
        raise ValueError(f"strategy is not in test panel (panel={panel})")
    if not _catalog_birth_test(strategy_id):
        raise ValueError("only catalog test strategies can be promoted")
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
    """Admin: move promoted strategy from Стратегии → Тестовые (enabled=false)."""
    apply_hidden_disable_once()
    strategy_id = validate_strategy(strategy_id)
    placement = ensure_strategy_ui_placement()
    panel = effective_ui_panel(strategy_id, placement)
    if panel != "main":
        raise ValueError(f"strategy is not in main panel (panel={panel})")
    if not _catalog_birth_test(strategy_id):
        raise ValueError("only catalog test strategies can be demoted to test")
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
        "ui_panel": "test",
        "enabled": False,
        "ui_placement": ui_placement_payload(placement),
        **get_strategies_payload(),
    }


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
    path = test_strategy_settings_file()
    raw: dict[str, Any] | None = None
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            raw = None
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
    path = test_strategy_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _normalize_test_strategy_settings(settings)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


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
    placement = ensure_strategy_ui_placement()
    return {
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
        "ui_placement": ui_placement_payload(placement),
    }


def toggle_strategy(strategy_id: str, enabled: bool) -> dict[str, Any]:
    try:
        strategy_id = validate_strategy(strategy_id)
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc
    apply_hidden_disable_once()
    panel = effective_ui_panel(strategy_id)
    if panel == "hidden":
        raise ValueError("strategy is hidden from UI panels")
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
    if scope not in ("all", "main", "test"):
        scope = "main" if enabled else "all"

    next_state: dict[str, bool] = dict(prev)
    user = tc.current_user()
    for s in AVAILABLE_STRATEGIES:
        sid = s["id"]
        if user and not tm.user_may_use_strategy(user, sid):
            next_state[sid] = False
            continue
        panel = effective_ui_panel(sid, placement)
        is_test = panel == "test"
        is_main = panel == "main"
        if panel == "hidden":
            # Hidden never participate in enable-all; stay off when disabling all.
            if not enabled and scope == "all":
                next_state[sid] = False
            continue
        if scope == "main" and not is_main:
            continue
        if scope == "test" and not is_test:
            continue
        if not enabled:
            next_state[sid] = False
        elif sid in legacy_ids:
            next_state[sid] = False
        elif is_test:
            next_state[sid] = True
        elif is_main:
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
    active = api_call(WHITELIST[bot])
    black = api_call(BLACKLIST[bot])
    max_trades = cfg.get("max_open_trades", 1)
    if max_trades == float("inf"):
        max_trades = INF_MAX_OPEN_TRADES
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
    if value < MIN_MAX_TRADES:
        raise ValueError(f"max_open_trades must be >= {MIN_MAX_TRADES} (0 = bot stopped)")

    cfg = load_config(CONFIGS[bot])
    old_val = int(cfg.get("max_open_trades", DEFAULT_BOT_LIMITS.get(bot, 2)))
    cfg["max_open_trades"] = value
    save_config(CONFIGS[bot], cfg)
    limits = load_bot_limits()
    limits[bot] = value
    save_bot_limits(limits)

    # Fast path: write disk, fire one reload, return. Long poll runs in background
    # so the UI save button is not stuck for 20–35s (and a concurrent enable
    # restart no longer races the poll).
    live_now = _live_max_open_trades(bot, timeout=2.0)
    reloads = 0
    live_applied = live_now == value
    reload_warning: str | None = None
    if live_now is None:
        reload_warning = (
            "бот недоступен — лимит записан на диск и применится после запуска"
        )
    elif not live_applied:
        try:
            api_call(
                RELOAD[bot],
                "POST",
                retries=1,
                timeout=min(LIMIT_RELOAD_TIMEOUT, 8.0),
            )
            reloads = 1
        except BaseException as exc:  # noqa: BLE001
            reload_warning = str(exc)
        live_after = _live_max_open_trades(bot, timeout=2.0)
        if live_after == value:
            live_applied = True
            live_now = live_after
            reload_warning = None
        else:
            if live_after is not None:
                live_now = live_after
            if not reload_warning:
                reload_warning = (
                    f"лимит {value} сохранён; бот ещё показывает "
                    f"{live_now if live_now is not None else '—'} — применяю в фоне"
                )
            threading.Thread(
                target=ensure_live_max_open_trades,
                args=(bot, value),
                name=f"ensure-max-trades-{bot}",
                daemon=True,
            ).start()

    reload_result: Any = {"reloads": reloads, "live_applied": live_applied}

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
        "live_applied": bool(live_applied),
        "live_max_open_trades": live_now,
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
    # Keep control-plane desired in sync with slot count (0 = off).
    # Enqueue only on 0↔N or when process is down — N→M must not restart a healthy bot.
    try:
        uid = "admin"
        user = tc.current_user()
        if user and not (tm.is_admin(user) and not tm.is_impersonating(user)):
            uid = str(user.get("id") or "admin")
        control_plane.ensure_init()
        want = value >= 1
        snap = control_plane.snapshot_bot(uid, bot)
        prev_want = bool(snap.get("desired") if snap.get("desired") is not None else snap.get("enabled"))
        observed = snap.get("observed") or {}
        process_up = bool(observed.get("process_up"))
        # Also recover if API port is closed even when observed lags.
        port = int(bot_reconcile.bot_port(uid, bot) or 0)
        api_up = bot_reconcile.port_open(port) if port else process_up
        need_enqueue = (want != prev_want) or (want and not (process_up or api_up))
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


# Primary path first (VPS systemd --logfile); later entries are local fallbacks.
LOG_SOURCES: dict[str, tuple[str, tuple[Path, ...]]] = {
    "executor": (
        "Исполнитель",
        (BASE / "user_data" / "logs" / "trade-executor.log",),
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


def _resolve_log_bots(requested: str) -> list[str]:
    if not requested or requested == "all":
        return list(LOG_SOURCES.keys())
    bots = [b.strip() for b in requested.split(",") if b.strip()]
    return [b for b in bots if b in LOG_SOURCES]


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


def trigger_strategy_scan() -> dict[str, Any]:
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
        if tm.is_impersonating(user):
            admin = tm.get_user_by_id(str(user.get("_imp_by") or ""))
            payload["impersonated_by"] = (
                {"id": admin.get("id"), "username": admin.get("username")}
                if admin
                else {"id": user.get("_imp_by")}
            )
        if tm.is_admin(user) and not tm.is_impersonating(user):
            payload["permission_catalog"] = tm.permission_catalog(AVAILABLE_STRATEGIES)
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
        is_status = method == "GET" and rest.rstrip("/") == "status"
        is_light = rest.rstrip("/") in ("show_config", "count", "ping", "version")
        # Heavy/wedged ctbot must not freeze the UI for 60s.
        if is_status:
            timeout = 2.0
        elif is_light:
            timeout = 3.0
        else:
            timeout = 60.0
        status, headers, body_out = tm.proxy_bot_request(
            self.user or {},
            bot,
            self.command,
            rest,
            query=query or None,
            body_bytes=body or None,
            content_type=self.headers.get("Content-Type"),
            timeout=timeout,
        )
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
                if not self._require_admin():
                    return
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                self._json(200, promote_strategy_to_main(str(strategy_id)))
            elif action == "demote_strategy_to_test":
                if not self._require_admin():
                    return
                strategy_id = data.get("strategy", "")
                if not strategy_id:
                    self._json(400, {"error": "strategy required"})
                    return
                self._json(200, demote_strategy_to_test(str(strategy_id)))
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
        if path.startswith("/bots/"):
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
                # Optional Mini App auto-link: validate initData and take user.id
                init_data = str(data.get("init_data") or data.get("initData") or "").strip()
                if init_data and not chat_id:
                    tg_user = tg_links.validate_webapp_init_data(init_data)
                    if not tg_user or not tg_user.get("id"):
                        self._json(400, {"error": "невалидные данные Telegram WebApp"})
                        return
                    chat_id = str(tg_user["id"])
                saved = tg_links.set_link(uid, chat_id, enabled=enabled)
                if chat_id:
                    try:
                        tg_links.send_message(
                            chat_id,
                            f"✅ CryptoTools: чат привязан к аккаунту «{uid}».\n"
                            "Сюда будут приходить уведомления о сделках на русском.",
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
    host = os.environ.get("PAIR_CONFIG_HOST", "127.0.0.1")
    port = int(os.environ.get("PAIR_CONFIG_PORT", "8090"))
    _server_log.info("pair-config server starting on %s:%s", host, port)
    start_adaptive_scan_scheduler(
        BASE,
        ranging_scan=trigger_ranging_scan,
        strategy_scan=trigger_strategy_scan,
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
