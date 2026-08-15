# pragma pylint: disable=missing-docstring, invalid-name
"""Combines signals from enabled sub-strategies (see user_data/enabled_strategies.json)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pandas import DataFrame

import talib.abstract as ta
from ctengine.persistence import Trade
from ctengine.strategy import IStrategy

from AdxDiCrossStrategy import AdxDiCrossStrategy
from AdxMacdVolComboStrategy import AdxMacdVolComboStrategy
from AdxMomentumStrategy import AdxMomentumStrategy
from AdxMomentumTestStrategy import AdxMomentumTestStrategy
from AltVolumeBreakoutStrategy import AltVolumeBreakoutStrategy
from AltVolumeBreakoutTestStrategy import AltVolumeBreakoutTestStrategy
from AroonCrossStrategy import AroonCrossStrategy
from AtrChannelBreakoutStrategy import AtrChannelBreakoutStrategy
from AtrChannelBreakoutTestStrategy import AtrChannelBreakoutTestStrategy
from AwesomeOscStrategy import AwesomeOscStrategy
from BbSqueezeBreakoutStrategy import BbSqueezeBreakoutStrategy
from BollingerRsiStrategy import BollingerRsiStrategy
from BollingerRsiTestStrategy import BollingerRsiTestStrategy
from ChaikinOscStrategy import ChaikinOscStrategy
from ChaikinOscTestStrategy import ChaikinOscTestStrategy
from CmfZeroCrossStrategy import CmfZeroCrossStrategy
from CmfZeroCrossTestStrategy import CmfZeroCrossTestStrategy
from CriptoPairsStrategy import CriptoPairsStrategy
from DonchianAdxVolComboStrategy import DonchianAdxVolComboStrategy
from DonchianAdxVolComboTestStrategy import DonchianAdxVolComboTestStrategy
from DonchianBreakoutStrategy import DonchianBreakoutStrategy
from DonchianBreakoutTestStrategy import DonchianBreakoutTestStrategy
from ElderRayStrategy import ElderRayStrategy
from ElderRayTestStrategy import ElderRayTestStrategy
from EmaRsiAtrComboStrategy import EmaRsiAtrComboStrategy
from EngulfingTrendStrategy import EngulfingTrendStrategy
from FibPullbackStrategy import FibPullbackStrategy
from HeikinAshiFlipStrategy import HeikinAshiFlipStrategy
from HmaPpoAtrComboStrategy import HmaPpoAtrComboStrategy
from IchimokuTkCrossStrategy import IchimokuTkCrossStrategy
from KeltnerBreakoutStrategy import KeltnerBreakoutStrategy
from KeltnerStochVolComboStrategy import KeltnerStochVolComboStrategy
from LiteIntradayStrategy import LiteIntradayStrategy
from LiteRangeStrategy import LiteRangeStrategy
from MacdEmaStrategy import MacdEmaStrategy
from MacdEmaTestStrategy import MacdEmaTestStrategy
from MfiReclaimStrategy import MfiReclaimStrategy
from ObvEmaCrossStrategy import ObvEmaCrossStrategy
from ObvEmaCrossTestStrategy import ObvEmaCrossTestStrategy
from PpoSignalStrategy import PpoSignalStrategy
from PpoSignalTestStrategy import PpoSignalTestStrategy
from PsaraFlipStrategy import PsaraFlipStrategy
from PsaraFlipTestStrategy import PsaraFlipTestStrategy
from RocMomentumStrategy import RocMomentumStrategy
from ScalpEmaCrossStrategy import ScalpEmaCrossStrategy
from ScalpEmaCrossTestStrategy import ScalpEmaCrossTestStrategy
from ScalpMacdHistStrategy import ScalpMacdHistStrategy
from SupertrendRsiObvComboStrategy import SupertrendRsiObvComboStrategy
from SupertrendStrategy import SupertrendStrategy
from SupertrendTestStrategy import SupertrendTestStrategy
from TemaCrossStrategy import TemaCrossStrategy
from TripleEmaStrategy import TripleEmaStrategy
from TrixSignalStrategy import TrixSignalStrategy
from VortexCrossStrategy import VortexCrossStrategy
from WilliamsRReclaimStrategy import WilliamsRReclaimStrategy

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
from ml.gate import allow_trade_entry, persist_entry_ml  # noqa: E402
from _sim_live import PROD_STRATEGY_MINIMAL_ROI, PROD_STRATEGY_STOPLOSS  # noqa: E402

from ctengine.strategy import stoploss_from_open

ENABLED_FILE = Path(
    os.environ.get(
        "CT_ENABLED_STRATEGIES",
        str(Path(__file__).resolve().parent.parent / "enabled_strategies.json"),
    )
)
# Same user_data dir as enabled_strategies (tenant-aware when CT_ENABLED_STRATEGIES is set).
_USER_DATA_DIR = ENABLED_FILE.parent
DUAL_HEDGE_FILE = Path(
    os.environ.get("CT_DUAL_HEDGE", str(_USER_DATA_DIR / "dual_hedge.json"))
)
MAX_PER_STRATEGY_FILE = Path(
    os.environ.get(
        "CT_MAX_OPEN_TRADES_PER_STRATEGY",
        str(_USER_DATA_DIR / "max_open_trades_per_strategy.json"),
    )
)
TEST_SETTINGS_FILE = Path(
    os.environ.get(
        "CT_TEST_STRATEGY_SETTINGS",
        str(_USER_DATA_DIR / "test_strategy_settings.json"),
    )
)
PLACEMENT_FILE = Path(
    os.environ.get(
        "CT_STRATEGY_UI_PLACEMENT",
        str(_USER_DATA_DIR / "strategy_ui_placement.json"),
    )
)
HEDGE_TAG_SUFFIX = ":hedge"
INV_TAG_SUFFIX = ":inv"

DEFAULT_TEST_SETTINGS: dict = {
    "max_open_trades": 3,
    "max_open_trades_per_strategy": 0,
    "stake_amount": 5.0,
    "stoploss": -0.03,
    "take_profit": 0.012,
}

# Birth-test catalog ids (fallback when placement file missing).
_FALLBACK_TEST_STRATEGY_TAGS = frozenset(
    {
        "PsaraFlipTestStrategy",
        "AtrChannelBreakoutTestStrategy",
        "AdxMomentumTestStrategy",
        "SupertrendTestStrategy",
        "CmfZeroCrossTestStrategy",
        "ScalpEmaCrossTestStrategy",
        "ChaikinOscTestStrategy",
        "DonchianBreakoutTestStrategy",
        "PpoSignalTestStrategy",
        "DonchianAdxVolComboTestStrategy",
        "ObvEmaCrossTestStrategy",
        "ElderRayTestStrategy",
        "AltVolumeBreakoutTestStrategy",
        "BollingerRsiTestStrategy",
        "MacdEmaTestStrategy",
    }
)


def load_test_strategy_tags() -> frozenset[str]:
    """Strategies currently in the Тестовые panel (not promoted to main)."""
    if not PLACEMENT_FILE.is_file():
        return _FALLBACK_TEST_STRATEGY_TAGS
    try:
        data = json.loads(PLACEMENT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _FALLBACK_TEST_STRATEGY_TAGS
    main = {str(x) for x in (data.get("main") or [])}
    # Birth-test ids that are not in main stay in the test panel.
    return frozenset(sid for sid in _FALLBACK_TEST_STRATEGY_TAGS if sid not in main)


# Deprecated name kept for imports; prefer load_test_strategy_tags().
TEST_STRATEGY_TAGS = _FALLBACK_TEST_STRATEGY_TAGS

STRATEGY_REGISTRY: dict[str, type[IStrategy]] = {
    "CriptoPairsStrategy": CriptoPairsStrategy,
    "SupertrendStrategy": SupertrendStrategy,
    "SupertrendTestStrategy": SupertrendTestStrategy,
    "MacdEmaStrategy": MacdEmaStrategy,
    "MacdEmaTestStrategy": MacdEmaTestStrategy,
    "FibPullbackStrategy": FibPullbackStrategy,
    "TripleEmaStrategy": TripleEmaStrategy,
    "BollingerRsiStrategy": BollingerRsiStrategy,
    "BollingerRsiTestStrategy": BollingerRsiTestStrategy,
    "AdxMomentumStrategy": AdxMomentumStrategy,
    "AdxMomentumTestStrategy": AdxMomentumTestStrategy,
    "LiteIntradayStrategy": LiteIntradayStrategy,
    "LiteRangeStrategy": LiteRangeStrategy,
    "AltVolumeBreakoutStrategy": AltVolumeBreakoutStrategy,
    "AltVolumeBreakoutTestStrategy": AltVolumeBreakoutTestStrategy,
    "PsaraFlipStrategy": PsaraFlipStrategy,
    "PsaraFlipTestStrategy": PsaraFlipTestStrategy,
    "AtrChannelBreakoutStrategy": AtrChannelBreakoutStrategy,
    "AtrChannelBreakoutTestStrategy": AtrChannelBreakoutTestStrategy,
    "CmfZeroCrossStrategy": CmfZeroCrossStrategy,
    "CmfZeroCrossTestStrategy": CmfZeroCrossTestStrategy,
    "ScalpEmaCrossStrategy": ScalpEmaCrossStrategy,
    "ScalpEmaCrossTestStrategy": ScalpEmaCrossTestStrategy,
    "ChaikinOscStrategy": ChaikinOscStrategy,
    "ChaikinOscTestStrategy": ChaikinOscTestStrategy,
    "DonchianBreakoutStrategy": DonchianBreakoutStrategy,
    "DonchianBreakoutTestStrategy": DonchianBreakoutTestStrategy,
    "PpoSignalStrategy": PpoSignalStrategy,
    "PpoSignalTestStrategy": PpoSignalTestStrategy,
    "DonchianAdxVolComboStrategy": DonchianAdxVolComboStrategy,
    "DonchianAdxVolComboTestStrategy": DonchianAdxVolComboTestStrategy,
    "ObvEmaCrossStrategy": ObvEmaCrossStrategy,
    "ObvEmaCrossTestStrategy": ObvEmaCrossTestStrategy,
    "ElderRayStrategy": ElderRayStrategy,
    "ElderRayTestStrategy": ElderRayTestStrategy,
    "ScalpMacdHistStrategy": ScalpMacdHistStrategy,
    "KeltnerBreakoutStrategy": KeltnerBreakoutStrategy,
    "HeikinAshiFlipStrategy": HeikinAshiFlipStrategy,
    "VortexCrossStrategy": VortexCrossStrategy,
    "AwesomeOscStrategy": AwesomeOscStrategy,
    "KeltnerStochVolComboStrategy": KeltnerStochVolComboStrategy,
    "TemaCrossStrategy": TemaCrossStrategy,
    "TrixSignalStrategy": TrixSignalStrategy,
    "RocMomentumStrategy": RocMomentumStrategy,
    "HmaPpoAtrComboStrategy": HmaPpoAtrComboStrategy,
    "WilliamsRReclaimStrategy": WilliamsRReclaimStrategy,
    "BbSqueezeBreakoutStrategy": BbSqueezeBreakoutStrategy,
    "EmaRsiAtrComboStrategy": EmaRsiAtrComboStrategy,
    "AroonCrossStrategy": AroonCrossStrategy,
    "AdxMacdVolComboStrategy": AdxMacdVolComboStrategy,
    "EngulfingTrendStrategy": EngulfingTrendStrategy,
    "AdxDiCrossStrategy": AdxDiCrossStrategy,
    "MfiReclaimStrategy": MfiReclaimStrategy,
    "IchimokuTkCrossStrategy": IchimokuTkCrossStrategy,
    "SupertrendRsiObvComboStrategy": SupertrendRsiObvComboStrategy,
}

# Per-tag risk for ML pack — SL/ROI as in player_scenarios (sim test).
TAG_RISK: dict[str, dict] = {
    "AltVolumeBreakoutStrategy": {
        "stoploss": -0.015,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "25": 0.006, "75": 0.0},
    "AltVolumeBreakoutTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    },
    "PsaraFlipStrategy": {
        "stoploss": -0.02,
        "tp": 0.022,
        "minimal_roi": {"0": 0.022, "120": 0.012, "360": 0.006, "720": 0.0},
    },
    "PsaraFlipTestStrategy": {
        "stoploss": -0.02,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "45": 0.008, "120": 0.005, "360": 0.0},
    },
    "AtrChannelBreakoutStrategy": {"stoploss": -0.02, "tp": 0.014, "minimal_roi": {"0": 0.014, "60": 0.007, "180": 0.0}},
    "AtrChannelBreakoutTestStrategy": {
        "stoploss": -0.02,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "45": 0.007, "120": 0.0},
    },
    "CmfZeroCrossStrategy": {"stoploss": -0.017, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "130": 0.0}},
    "CmfZeroCrossTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "ScalpEmaCrossStrategy": {"stoploss": -0.01, "tp": 0.008, "minimal_roi": {"0": 0.008, "20": 0.004, "60": 0.0}},
    "ScalpEmaCrossTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "ChaikinOscStrategy": {"stoploss": -0.017, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "130": 0.0}},
    "ChaikinOscTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "DonchianBreakoutStrategy": {"stoploss": -0.025, "tp": 0.018, "minimal_roi": {"0": 0.018, "90": 0.009, "240": 0.0}},
    "DonchianBreakoutTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "PpoSignalStrategy": {"stoploss": -0.016, "tp": 0.01, "minimal_roi": {"0": 0.01, "40": 0.005, "120": 0.0}},
    "PpoSignalTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "DonchianAdxVolComboStrategy": {"stoploss": -0.022, "tp": 0.018, "minimal_roi": {"0": 0.018, "70": 0.009, "200": 0.0}},
    "DonchianAdxVolComboTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "ObvEmaCrossStrategy": {"stoploss": -0.018, "tp": 0.012, "minimal_roi": {"0": 0.012, "55": 0.006, "160": 0.0}},
    "ObvEmaCrossTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "ElderRayStrategy": {"stoploss": -0.017, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "130": 0.0}},
    "ElderRayTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "ScalpMacdHistStrategy": {"stoploss": -0.012, "tp": 0.009, "minimal_roi": {"0": 0.009, "30": 0.0045, "90": 0.0}},
    "KeltnerBreakoutStrategy": {"stoploss": -0.02, "tp": 0.015, "minimal_roi": {"0": 0.015, "60": 0.008, "180": 0.0}},
    "HeikinAshiFlipStrategy": {"stoploss": -0.016, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "120": 0.0}},
    "VortexCrossStrategy": {"stoploss": -0.019, "tp": 0.013, "minimal_roi": {"0": 0.013, "55": 0.0065, "160": 0.0}},
    "AwesomeOscStrategy": {"stoploss": -0.017, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "130": 0.0}},
    "KeltnerStochVolComboStrategy": {"stoploss": -0.02, "tp": 0.015, "minimal_roi": {"0": 0.015, "55": 0.0075, "160": 0.0}},
    "TemaCrossStrategy": {"stoploss": -0.018, "tp": 0.012, "minimal_roi": {"0": 0.012, "50": 0.006, "150": 0.0}},
    "TrixSignalStrategy": {"stoploss": -0.017, "tp": 0.011, "minimal_roi": {"0": 0.011, "45": 0.0055, "130": 0.0}},
    "RocMomentumStrategy": {"stoploss": -0.016, "tp": 0.01, "minimal_roi": {"0": 0.01, "40": 0.005, "120": 0.0}},
    "HmaPpoAtrComboStrategy": {"stoploss": -0.018, "tp": 0.014, "minimal_roi": {"0": 0.014, "50": 0.007, "150": 0.0}},
    "WilliamsRReclaimStrategy": {"stoploss": -0.015, "tp": 0.01, "minimal_roi": {"0": 0.01, "40": 0.005, "100": 0.0}},
    "BbSqueezeBreakoutStrategy": {"stoploss": -0.018, "tp": 0.013, "minimal_roi": {"0": 0.013, "50": 0.006, "150": 0.0}},
    "EmaRsiAtrComboStrategy": {"stoploss": -0.018, "tp": 0.014, "minimal_roi": {"0": 0.014, "50": 0.007, "150": 0.0}},
    "AroonCrossStrategy": {"stoploss": -0.018, "tp": 0.012, "minimal_roi": {"0": 0.012, "50": 0.006, "150": 0.0}},
    "AdxMacdVolComboStrategy": {"stoploss": -0.02, "tp": 0.016, "minimal_roi": {"0": 0.016, "60": 0.008, "180": 0.0}},
    "EngulfingTrendStrategy": {"stoploss": -0.016, "tp": 0.011, "minimal_roi": {"0": 0.011, "40": 0.0055, "110": 0.0}},
    "AdxDiCrossStrategy": {"stoploss": -0.02, "tp": 0.014, "minimal_roi": {"0": 0.014, "60": 0.007, "180": 0.0}},
    "MfiReclaimStrategy": {"stoploss": -0.015, "tp": 0.01, "minimal_roi": {"0": 0.01, "40": 0.005, "110": 0.0}},
    "IchimokuTkCrossStrategy": {"stoploss": -0.022, "tp": 0.016, "minimal_roi": {"0": 0.016, "80": 0.008, "200": 0.0}},
    "SupertrendRsiObvComboStrategy": {"stoploss": -0.019, "tp": 0.015, "minimal_roi": {"0": 0.015, "55": 0.0075, "160": 0.0}},
    # Legacy Jul set — april-cut retrain (appended to pack as #32–39)
    "AdxMomentumStrategy": {
        "stoploss": -0.022,
        "tp": 0.03,
        "minimal_roi": {"0": 0.03, "180": 0.015, "480": 0.008, "960": 0},
    },
    "AdxMomentumTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.02,
        "minimal_roi": {"0": 0.02, "60": 0.012, "180": 0.008, "480": 0},
    },
    "BollingerRsiStrategy": {
        "stoploss": -0.02,
        "tp": 0.025,
        "minimal_roi": {"0": 0.025, "60": 0.015, "180": 0.008, "720": 0},
    "BollingerRsiTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    },
    "MacdEmaStrategy": {
        "stoploss": -0.025,
        "tp": 0.03,
        "minimal_roi": {"0": 0.03, "180": 0.015, "480": 0.008, "960": 0},
    "MacdEmaTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    },
    "SupertrendStrategy": {
        "stoploss": -0.025,
        "tp": 0.03,
        "minimal_roi": {"0": 0.03, "120": 0.015, "360": 0.008, "720": 0},
    },
    "SupertrendTestStrategy": {
        "stoploss": -0.03,
        "tp": 0.012,
        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},
    },
    "TripleEmaStrategy": {
        "stoploss": -0.025,
        "tp": 0.04,
        "minimal_roi": {"0": 0.04, "240": 0.02, "720": 0.01, "1440": 0},
    },
    "LiteRangeStrategy": {
        "stoploss": -0.018,
        "tp": 0.022,
        "minimal_roi": {"0": 0.022, "40": 0.012, "100": 0},
    },
    "LiteIntradayStrategy": {
        "stoploss": -0.022,
        "tp": 0.022,
        "minimal_roi": {"0": 0.022, "480": 0.012, "960": 0},
    },
    "FibPullbackStrategy": {
        "stoploss": -0.03,
        "tp": 0.035,
        "minimal_roi": {"0": 0.035, "360": 0.02, "720": 0.01, "1440": 0},
    },
}

MEAN_REV_ADX_TAGS = frozenset({"BollingerRsiStrategy", "BollingerRsiTestStrategy", "LiteRangeStrategy", "CriptoPairsStrategy"})

SCENARIO_BY_TAG: dict[str, dict[str, str]] = {
    "TripleEmaStrategy": {
        "scenario_id": "trend_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "TripleEmaStrategy",
        "label": "EMA 50/200 (4H)",
    },
    "BollingerRsiStrategy": {
        "scenario_id": "lite_mean_rev",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "BollingerRsiStrategy",
        "label": "Mean-reversion (BB)",
    },
    "BollingerRsiTestStrategy": {
        "scenario_id": "lite_mean_rev_test",
        "scan_type": "strategy",
        "group": "lite_test",
        "strategy": "BollingerRsiTestStrategy",
        "label": "Mean-reversion (BB) (test) wide",
    },
    "AdxMomentumStrategy": {
        "scenario_id": "trend_breakout",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "AdxMomentumStrategy",
        "label": "Breakout-Retest",
    },
    "AdxMomentumTestStrategy": {
        "scenario_id": "trend_breakout_test",
        "scan_type": "strategy",
        "group": "trend_test",
        "strategy": "AdxMomentumTestStrategy",
        "label": "Breakout-Retest (test wide)",
    },
    "LiteIntradayStrategy": {
        "scenario_id": "lite_intraday",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteIntradayStrategy",
        "label": "Внутридневная",
    },
    "LiteRangeStrategy": {
        "scenario_id": "lite_range",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteRangeStrategy",
        "label": "Диапазонная",
    },
    "SupertrendStrategy": {
        "scenario_id": "trend_supertrend",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "SupertrendStrategy",
        "label": "Supertrend (ATR) (ML Gate)",
    },
    "SupertrendTestStrategy": {
        "scenario_id": "trend_supertrend_test",
        "scan_type": "strategy",
        "group": "trend_test",
        "strategy": "SupertrendTestStrategy",
        "label": "Supertrend (ATR) (test wide)",
    },
    "MacdEmaStrategy": {
        "scenario_id": "trend_macd_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "MacdEmaStrategy",
        "label": "MACD + EMA200 (ML Gate)",
    },
    "MacdEmaTestStrategy": {
        "scenario_id": "trend_macd_ema_test",
        "scan_type": "strategy",
        "group": "trend_test",
        "strategy": "MacdEmaTestStrategy",
        "label": "MACD + EMA200 (test) wide",
    },
    "FibPullbackStrategy": {
        "scenario_id": "trend_fib",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "FibPullbackStrategy",
        "label": "Fib pullback (DCA) (ML Gate)",
    },
    "AltVolumeBreakoutStrategy": {
        "scenario_id": "scalp_liq_breakout",
        "scan_type": "strategy",
        "group": "scalp_liq",
        "strategy": "AltVolumeBreakoutStrategy",
        "label": "Alt volume breakout",
    },
    "AltVolumeBreakoutTestStrategy": {
        "scenario_id": "scalp_liq_breakout_test",
        "scan_type": "strategy",
        "group": "scalp_liq_test",
        "strategy": "AltVolumeBreakoutTestStrategy",
        "label": "Alt volume breakout (test) wide",
    },
    "PsaraFlipStrategy": {
        "scenario_id": "new_psar",
        "scan_type": "strategy",
        "group": "newset",
        "strategy": "PsaraFlipStrategy",
        "label": "Parabolic SAR flip",
    },
    "PsaraFlipTestStrategy": {
        "scenario_id": "new_psar_test",
        "scan_type": "strategy",
        "group": "newset_test",
        "strategy": "PsaraFlipTestStrategy",
        "label": "Parabolic SAR flip (test wide)",
    },
    "AtrChannelBreakoutStrategy": {
        "scenario_id": "chart3_atrch",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "AtrChannelBreakoutStrategy",
        "label": "ATR channel breakout",
    },
    "AtrChannelBreakoutTestStrategy": {
        "scenario_id": "chart3_atrch_test",
        "scan_type": "strategy",
        "group": "chart3_test",
        "strategy": "AtrChannelBreakoutTestStrategy",
        "label": "ATR channel breakout (test wide)",
    },
    "CmfZeroCrossStrategy": {
        "scenario_id": "chart2_cmf",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "CmfZeroCrossStrategy",
        "label": "CMF zero cross",
    },
    "CmfZeroCrossTestStrategy": {
        "scenario_id": "chart2_cmf_test",
        "scan_type": "strategy",
        "group": "chart2_test",
        "strategy": "CmfZeroCrossTestStrategy",
        "label": "CMF zero cross (test wide)",
    },
    "ScalpEmaCrossStrategy": {
        "scenario_id": "scalp_ema",
        "scan_type": "strategy",
        "group": "scalp",
        "strategy": "ScalpEmaCrossStrategy",
        "label": "Scalp EMA 8/21",
    },
    "ScalpEmaCrossTestStrategy": {
        "scenario_id": "scalp_ema_test",
        "scan_type": "strategy",
        "group": "scalp_test",
        "strategy": "ScalpEmaCrossTestStrategy",
        "label": "Scalp EMA 8/21 (test) wide",
    },
    "ChaikinOscStrategy": {
        "scenario_id": "chart3_adosc",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "ChaikinOscStrategy",
        "label": "Chaikin Oscillator",
    },
    "ChaikinOscTestStrategy": {
        "scenario_id": "chart3_adosc_test",
        "scan_type": "strategy",
        "group": "chart3_test",
        "strategy": "ChaikinOscTestStrategy",
        "label": "Chaikin Oscillator (test) wide",
    },
    "DonchianBreakoutStrategy": {
        "scenario_id": "new_donchian",
        "scan_type": "strategy",
        "group": "newset",
        "strategy": "DonchianBreakoutStrategy",
        "label": "Donchian / Turtle",
    },
    "DonchianBreakoutTestStrategy": {
        "scenario_id": "new_donchian_test",
        "scan_type": "strategy",
        "group": "newset_test",
        "strategy": "DonchianBreakoutTestStrategy",
        "label": "Donchian / Turtle (test) wide",
    },
    "PpoSignalStrategy": {
        "scenario_id": "chart3_ppo",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "PpoSignalStrategy",
        "label": "PPO signal cross",
    },
    "PpoSignalTestStrategy": {
        "scenario_id": "chart3_ppo_test",
        "scan_type": "strategy",
        "group": "chart3_test",
        "strategy": "PpoSignalTestStrategy",
        "label": "PPO signal cross (test) wide",
    },
    "DonchianAdxVolComboStrategy": {
        "scenario_id": "combo_don_adx_vol",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "DonchianAdxVolComboStrategy",
        "label": "Donchian+ADX+Vol",
    },
    "DonchianAdxVolComboTestStrategy": {
        "scenario_id": "combo_don_adx_vol_test",
        "scan_type": "strategy",
        "group": "combo_test",
        "strategy": "DonchianAdxVolComboTestStrategy",
        "label": "Donchian+ADX+Vol (test) wide",
    },
    "ObvEmaCrossStrategy": {
        "scenario_id": "chart2_obv",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "ObvEmaCrossStrategy",
        "label": "OBV EMA cross",
    },
    "ObvEmaCrossTestStrategy": {
        "scenario_id": "chart2_obv_test",
        "scan_type": "strategy",
        "group": "chart2_test",
        "strategy": "ObvEmaCrossTestStrategy",
        "label": "OBV EMA cross (test) wide",
    },
    "ElderRayStrategy": {
        "scenario_id": "chart3_elder",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "ElderRayStrategy",
        "label": "Elder Ray Bull/Bear",
    },
    "ElderRayTestStrategy": {
        "scenario_id": "chart3_elder_test",
        "scan_type": "strategy",
        "group": "chart3_test",
        "strategy": "ElderRayTestStrategy",
        "label": "Elder Ray Bull/Bear (test) wide",
    },
    "ScalpMacdHistStrategy": {
        "scenario_id": "scalp_macd",
        "scan_type": "strategy",
        "group": "scalp",
        "strategy": "ScalpMacdHistStrategy",
        "label": "Scalp MACD hist",
    },
    "KeltnerBreakoutStrategy": {
        "scenario_id": "new_keltner",
        "scan_type": "strategy",
        "group": "newset",
        "strategy": "KeltnerBreakoutStrategy",
        "label": "Keltner breakout",
    },
    "HeikinAshiFlipStrategy": {
        "scenario_id": "chart_ha",
        "scan_type": "strategy",
        "group": "chart",
        "strategy": "HeikinAshiFlipStrategy",
        "label": "Heikin Ashi flip",
    },
    "VortexCrossStrategy": {
        "scenario_id": "chart2_vortex",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "VortexCrossStrategy",
        "label": "Vortex VI+/VI−",
    },
    "AwesomeOscStrategy": {
        "scenario_id": "chart3_ao",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "AwesomeOscStrategy",
        "label": "Awesome Oscillator",
    },
    "KeltnerStochVolComboStrategy": {
        "scenario_id": "combo_kc_stoch_vol",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "KeltnerStochVolComboStrategy",
        "label": "Keltner+Stoch+Vol",
    },
    "TemaCrossStrategy": {
        "scenario_id": "chart3_tema",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "TemaCrossStrategy",
        "label": "TEMA fast/slow",
    },
    "TrixSignalStrategy": {
        "scenario_id": "chart2_trix",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "TrixSignalStrategy",
        "label": "TRIX signal cross",
    },
    "RocMomentumStrategy": {
        "scenario_id": "chart3_roc",
        "scan_type": "strategy",
        "group": "chart3",
        "strategy": "RocMomentumStrategy",
        "label": "ROC momentum",
    },
    "HmaPpoAtrComboStrategy": {
        "scenario_id": "combo_hma_ppo_atr",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "HmaPpoAtrComboStrategy",
        "label": "HMA+PPO+ATR",
    },
    "WilliamsRReclaimStrategy": {
        "scenario_id": "chart_willr",
        "scan_type": "strategy",
        "group": "chart",
        "strategy": "WilliamsRReclaimStrategy",
        "label": "Williams %R reclaim",
    },
    "BbSqueezeBreakoutStrategy": {
        "scenario_id": "chart_squeeze",
        "scan_type": "strategy",
        "group": "chart",
        "strategy": "BbSqueezeBreakoutStrategy",
        "label": "BB squeeze breakout",
    },
    "EmaRsiAtrComboStrategy": {
        "scenario_id": "combo_ema_rsi_atr",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "EmaRsiAtrComboStrategy",
        "label": "EMA+RSI+ATR triad",
    },
    "AroonCrossStrategy": {
        "scenario_id": "chart2_aroon",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "AroonCrossStrategy",
        "label": "Aroon Up/Down cross",
    },
    "AdxMacdVolComboStrategy": {
        "scenario_id": "combo_adx_macd_vol",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "AdxMacdVolComboStrategy",
        "label": "ADX+MACD+Vol",
    },
    "EngulfingTrendStrategy": {
        "scenario_id": "chart2_engulf",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "EngulfingTrendStrategy",
        "label": "Engulfing + EMA",
    },
    "AdxDiCrossStrategy": {
        "scenario_id": "chart_adxdi",
        "scan_type": "strategy",
        "group": "chart",
        "strategy": "AdxDiCrossStrategy",
        "label": "ADX DI+/DI− cross",
    },
    "MfiReclaimStrategy": {
        "scenario_id": "chart2_mfi",
        "scan_type": "strategy",
        "group": "chart2",
        "strategy": "MfiReclaimStrategy",
        "label": "MFI reclaim",
    },
    "IchimokuTkCrossStrategy": {
        "scenario_id": "new_ichimoku",
        "scan_type": "strategy",
        "group": "newset",
        "strategy": "IchimokuTkCrossStrategy",
        "label": "Ichimoku TK cross",
    },
    "SupertrendRsiObvComboStrategy": {
        "scenario_id": "combo_st_rsi_obv",
        "scan_type": "strategy",
        "group": "combo",
        "strategy": "SupertrendRsiObvComboStrategy",
        "label": "Supertrend+RSI+OBV",
    },
}


def load_enabled_map() -> dict[str, bool]:
    if not ENABLED_FILE.is_file():
        return {sid: sid == "CriptoPairsStrategy" for sid in STRATEGY_REGISTRY}
    data = json.loads(ENABLED_FILE.read_text(encoding="utf-8"))
    enabled = data.get("enabled", {})
    return {sid: bool(enabled.get(sid, False)) for sid in STRATEGY_REGISTRY}


def load_inverted_map() -> dict[str, bool]:
    if not ENABLED_FILE.is_file():
        return {sid: False for sid in STRATEGY_REGISTRY}
    try:
        data = json.loads(ENABLED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {sid: False for sid in STRATEGY_REGISTRY}
    inverted = data.get("inverted", {})
    return {sid: bool(inverted.get(sid, False)) for sid in STRATEGY_REGISTRY}


def load_trained_risk_map() -> dict[str, bool]:
    """Per-strategy: use TAG_RISK (sim/train SL+ROI) when True; else global config risk."""
    defaults = {sid: (sid in TAG_RISK) for sid in STRATEGY_REGISTRY}
    if not ENABLED_FILE.is_file():
        return defaults
    try:
        data = json.loads(ENABLED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return defaults
    stored = data.get("trained_risk") or {}
    out = dict(defaults)
    for sid in STRATEGY_REGISTRY:
        if sid in stored:
            out[sid] = bool(stored[sid])
        # Strategies without TAG_RISK cannot use trained risk.
        if sid not in TAG_RISK:
            out[sid] = False
    return out


def tag_uses_trained_risk(tag: str) -> bool:
    if tag not in TAG_RISK:
        return False
    return bool(load_trained_risk_map().get(tag, True))


def load_dual_hedge_enabled() -> bool:
    if not DUAL_HEDGE_FILE.is_file():
        return False
    try:
        data = json.loads(DUAL_HEDGE_FILE.read_text(encoding="utf-8"))
        return bool(data.get("enabled", False))
    except (json.JSONDecodeError, OSError):
        return False


def load_max_open_trades_per_strategy() -> int:
    """Max concurrent open trades sharing the same strategy enter_tag. 0 = unlimited."""
    if not MAX_PER_STRATEGY_FILE.is_file():
        return 0
    try:
        data = json.loads(MAX_PER_STRATEGY_FILE.read_text(encoding="utf-8"))
        return max(0, int(data.get("value", 0)))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def load_test_strategy_settings() -> dict:
    """Isolated test-block limits / stake / fallback SL-TP (no bot reload)."""
    out = dict(DEFAULT_TEST_SETTINGS)
    if not TEST_SETTINGS_FILE.is_file():
        return out
    try:
        data = json.loads(TEST_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    try:
        if "max_open_trades" in data:
            out["max_open_trades"] = max(0, int(data["max_open_trades"]))
        if "max_open_trades_per_strategy" in data:
            out["max_open_trades_per_strategy"] = max(0, int(data["max_open_trades_per_strategy"]))
        if "stake_amount" in data:
            out["stake_amount"] = float(data["stake_amount"])
        if "stoploss" in data:
            out["stoploss"] = -abs(float(data["stoploss"]))
        if "take_profit" in data:
            out["take_profit"] = abs(float(data["take_profit"]))
    except (TypeError, ValueError):
        return dict(DEFAULT_TEST_SETTINGS)
    return out


def is_test_strategy_tag(tag: str | None) -> bool:
    return bool(tag) and str(tag) in load_test_strategy_tags()


def count_open_trades_for_tag(tag: str) -> int:
    if not tag:
        return 0
    n = 0
    for trade in Trade.get_open_trades():
        if base_enter_tag(trade.enter_tag) == tag:
            n += 1
    return n


def count_open_test_trades() -> int:
    n = 0
    for trade in Trade.get_open_trades():
        if is_test_strategy_tag(base_enter_tag(trade.enter_tag)):
            n += 1
    return n


def base_enter_tag(tag: str | None) -> str:
    """Strip :inv / :hedge suffixes to get the strategy id."""
    if not tag:
        return ""
    t = str(tag)
    changed = True
    while changed:
        changed = False
        for suffix in (HEDGE_TAG_SUFFIX, INV_TAG_SUFFIX):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
                changed = True
    return t


class MultiStrategyRouter(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 250

    minimal_roi = PROD_STRATEGY_MINIMAL_ROI
    stoploss = PROD_STRATEGY_STOPLOSS
    trailing_stop = False
    # Must be True: ctengine only calls custom_exit inside the use_exit_signal branch.
    # populate_exit_trend stays empty — exits come from per-tag custom_exit (SAR flip / retest_fail).
    use_exit_signal = True
    use_custom_stoploss = True
    use_custom_roi = True
    use_custom_exit = True

    # Block entries when market is trending (mean-reversion only)
    adx_max_entry = 25

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._instances: dict[str, IStrategy] = {}
        sl = config.get("stoploss")
        if sl is not None:
            self.stoploss = float(sl)
        roi = config.get("minimal_roi")
        if isinstance(roi, dict) and roi:
            self.minimal_roi = dict(roi)

    @property
    def dual_hedge_entries(self) -> bool:
        """When True, each entry signal opens primary + opposite hedge leg."""
        return load_dual_hedge_enabled()

    def _enabled_ids(self) -> list[str]:
        enabled = load_enabled_map()
        return [sid for sid, on in enabled.items() if on]

    def _get_instance(self, strategy_id: str) -> IStrategy:
        if strategy_id not in self._instances:
            self._instances[strategy_id] = STRATEGY_REGISTRY[strategy_id](self.config)
        return self._instances[strategy_id]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        inverted = load_inverted_map()
        for sid in self._enabled_ids():
            strat = self._get_instance(sid)
            df = strat.populate_indicators(dataframe.copy(), metadata)
            df = strat.populate_entry_trend(df, metadata)

            long_mask = df.get("enter_long", 0).fillna(0).astype(int) == 1
            short_mask = df.get("enter_short", 0).fillna(0).astype(int) == 1
            if inverted.get(sid):
                long_mask, short_mask = short_mask, long_mask
                tag = f"{sid}{INV_TAG_SUFFIX}"
            else:
                tag = sid

            first_long = long_mask & (dataframe["enter_long"] != 1)
            first_short = short_mask & (dataframe["enter_short"] != 1)
            dataframe.loc[first_long, "enter_tag"] = tag
            dataframe.loc[first_short, "enter_tag"] = tag
            dataframe.loc[long_mask, "enter_long"] = 1
            dataframe.loc[short_mask, "enter_short"] = 1

        ranging = dataframe["adx"] < self.adx_max_entry
        base_tags = (
            dataframe["enter_tag"]
            .astype(str)
            .map(lambda t: base_enter_tag(t) if t and t != "nan" else "")
        )
        for side_col in ("enter_long", "enter_short"):
            mask = (dataframe[side_col] == 1) & base_tags.isin(MEAN_REV_ADX_TAGS)
            block = mask & ~ranging
            dataframe.loc[block, side_col] = 0
            dataframe.loc[block, "enter_tag"] = ""

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool | None:
        """Per-tag exits (SAR flip / failed retest) without global exit_signal."""
        tag = base_enter_tag(trade.enter_tag)
        if tag not in STRATEGY_REGISTRY:
            return None
        inst = self._get_instance(tag)
        fn = getattr(inst, "exit_reason_from_ohlcv", None)
        if not callable(fn):
            return None
        try:
            df = self.dp.get_pair_dataframe(pair, self.timeframe)
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True):
            try:
                df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            except Exception:
                return None
        if df is None or getattr(df, "empty", True):
            return None
        return fn(df, trade, current_rate)

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag,
        side: str,
        **kwargs,
    ) -> float:
        tag = base_enter_tag(entry_tag)
        if tag in STRATEGY_REGISTRY:
            return self._get_instance(tag).leverage(
                pair, current_time, current_rate, proposed_leverage, max_leverage, tag, side, **kwargs
            )
        return min(3.0, max_leverage)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        tag = base_enter_tag(entry_tag)
        if not is_test_strategy_tag(tag):
            return proposed_stake
        stake = float(load_test_strategy_settings().get("stake_amount") or proposed_stake)
        if min_stake is not None:
            stake = max(float(min_stake), stake)
        return min(stake, float(max_stake))

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        tag = base_enter_tag(trade.enter_tag)
        if tag_uses_trained_risk(tag):
            risk = TAG_RISK.get(tag)
            if not risk:
                return None
            return stoploss_from_open(
                float(risk["stoploss"]),
                current_profit,
                is_short=trade.is_short,
                leverage=float(trade.leverage or 1.0),
            )
        if is_test_strategy_tag(tag):
            sl = float(load_test_strategy_settings().get("stoploss") or DEFAULT_TEST_SETTINGS["stoploss"])
            return stoploss_from_open(
                sl,
                current_profit,
                is_short=trade.is_short,
                leverage=float(trade.leverage or 1.0),
            )
        return None

    def custom_roi(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        trade_duration: int,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | None:
        tag = base_enter_tag(entry_tag or trade.enter_tag)
        if tag_uses_trained_risk(tag):
            risk = TAG_RISK.get(tag)
            if not risk:
                return None
            roi_map = risk.get("minimal_roi")
            if isinstance(roi_map, dict) and roi_map:
                keys = sorted((int(k), float(v)) for k, v in roi_map.items())
                chosen = float(keys[0][1])
                for mins, val in keys:
                    if trade_duration >= mins:
                        chosen = val
                return chosen
            return float(risk["tp"])
        if is_test_strategy_tag(tag):
            return float(
                load_test_strategy_settings().get("take_profit")
                or DEFAULT_TEST_SETTINGS["take_profit"]
            )
        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        tag = base_enter_tag(entry_tag)
        # Hedge leg always follows the primary — do not re-check ML / cooldown / per-tag cap.
        if entry_tag and HEDGE_TAG_SUFFIX in str(entry_tag):
            return True
        test_tag = is_test_strategy_tag(tag)
        if test_tag:
            test_cfg = load_test_strategy_settings()
            test_cap = int(test_cfg.get("max_open_trades") or 0)
            if test_cap <= 0 or count_open_test_trades() >= test_cap:
                return False
            per_test = int(test_cfg.get("max_open_trades_per_strategy") or 0)
            if per_test > 0 and tag and count_open_trades_for_tag(tag) >= per_test:
                return False
        else:
            per_strat_limit = load_max_open_trades_per_strategy()
            if per_strat_limit > 0 and tag and count_open_trades_for_tag(tag) >= per_strat_limit:
                return False
        if tag in STRATEGY_REGISTRY:
            inst = self._get_instance(tag)
            cooldown = getattr(inst, "pair_in_cooldown", None)
            if callable(cooldown) and cooldown(pair, current_time):
                return False
        scenario = SCENARIO_BY_TAG.get(tag)
        if not scenario:
            return True
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if test_tag:
            test_cfg = load_test_strategy_settings()
            stake = float(test_cfg.get("stake_amount") or self.config.get("stake_amount") or 0)
        else:
            stake = float(self.config.get("stake_amount") or 0)
        if tag_uses_trained_risk(tag):
            risk = TAG_RISK.get(tag) or {}
            sl = float(risk.get("stoploss", self.stoploss))
            tp = risk.get("tp")
            if isinstance(risk.get("minimal_roi"), dict) and risk["minimal_roi"]:
                roi = {str(k): float(v) for k, v in risk["minimal_roi"].items()}
            else:
                roi = {"0": float(tp)} if tp is not None else dict(self.minimal_roi)
        elif test_tag:
            test_cfg = load_test_strategy_settings()
            sl = float(test_cfg.get("stoploss") or self.stoploss)
            roi = {"0": float(test_cfg.get("take_profit") or 0.05)}
        else:
            sl = float(self.stoploss)
            roi = dict(self.minimal_roi)
        return allow_trade_entry(
            scenario=scenario,
            pair=pair,
            rate=rate,
            side=side,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=sl,
            minimal_roi=roi,
            timeframe=self.timeframe,
            ohlcv_df=df,
        )

    def order_filled(
        self,
        pair: str,
        trade: Trade,
        order,
        current_time: datetime,
        **kwargs,
    ) -> None:
        if order.ft_order_side != trade.entry_side:
            return
        tag = base_enter_tag(trade.enter_tag)
        scenario = SCENARIO_BY_TAG.get(tag)
        if not scenario:
            return
        test_tag = is_test_strategy_tag(tag)
        if test_tag:
            test_cfg = load_test_strategy_settings()
            stake = float(test_cfg.get("stake_amount") or self.config.get("stake_amount") or 0)
        else:
            stake = float(self.config.get("stake_amount") or 0)
        if tag_uses_trained_risk(tag):
            risk = TAG_RISK.get(tag) or {}
            sl = float(risk.get("stoploss", self.stoploss))
            tp = risk.get("tp")
            if isinstance(risk.get("minimal_roi"), dict) and risk["minimal_roi"]:
                roi = {str(k): float(v) for k, v in risk["minimal_roi"].items()}
            else:
                roi = {"0": float(tp)} if tp is not None else dict(self.minimal_roi)
        elif test_tag:
            test_cfg = load_test_strategy_settings()
            sl = float(test_cfg.get("stoploss") or self.stoploss)
            roi = {"0": float(test_cfg.get("take_profit") or 0.05)}
        else:
            sl = float(self.stoploss)
            roi = dict(self.minimal_roi)
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        persist_entry_ml(
            trade,
            scenario=scenario,
            ohlcv_df=df,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=sl,
            minimal_roi=roi,
            timeframe=self.timeframe,
        )
