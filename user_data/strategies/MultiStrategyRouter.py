# pragma pylint: disable=missing-docstring, invalid-name
"""Combines signals from enabled sub-strategies (see user_data/enabled_strategies.json)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

from AdxMomentumStrategy import AdxMomentumStrategy
from BollingerRsiStrategy import BollingerRsiStrategy
from CriptoPairsStrategy import CriptoPairsStrategy
from FibPullbackStrategy import FibPullbackStrategy
from LiteIntradayStrategy import LiteIntradayStrategy
from LiteRangeStrategy import LiteRangeStrategy
from MacdEmaStrategy import MacdEmaStrategy
from SupertrendStrategy import SupertrendStrategy
from TripleEmaStrategy import TripleEmaStrategy

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
from ml.gate import allow_trade_entry, pop_entry_ml, save_ml_to_trade  # noqa: E402
from _sim_live import PROD_STRATEGY_MINIMAL_ROI, PROD_STRATEGY_STOPLOSS  # noqa: E402

ENABLED_FILE = Path(__file__).resolve().parent.parent / "enabled_strategies.json"
DUAL_HEDGE_FILE = Path(__file__).resolve().parent.parent / "dual_hedge.json"
HEDGE_TAG_SUFFIX = ":hedge"
INV_TAG_SUFFIX = ":inv"

STRATEGY_REGISTRY: dict[str, type[IStrategy]] = {
    "CriptoPairsStrategy": CriptoPairsStrategy,
    "SupertrendStrategy": SupertrendStrategy,
    "MacdEmaStrategy": MacdEmaStrategy,
    "FibPullbackStrategy": FibPullbackStrategy,
    "TripleEmaStrategy": TripleEmaStrategy,
    "BollingerRsiStrategy": BollingerRsiStrategy,
    "AdxMomentumStrategy": AdxMomentumStrategy,
    "LiteIntradayStrategy": LiteIntradayStrategy,
    "LiteRangeStrategy": LiteRangeStrategy,
}

# ADX cap applies only to mean-reversion entries (not trend/momentum/intraday).
MEAN_REV_ADX_TAGS = frozenset({"BollingerRsiStrategy", "LiteRangeStrategy", "CriptoPairsStrategy"})

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
    "AdxMomentumStrategy": {
        "scenario_id": "trend_breakout",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "AdxMomentumStrategy",
        "label": "Breakout-Retest",
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
    "MacdEmaStrategy": {
        "scenario_id": "trend_macd_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "MacdEmaStrategy",
        "label": "MACD + EMA200 (ML Gate)",
    },
    "FibPullbackStrategy": {
        "scenario_id": "trend_fib",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "FibPullbackStrategy",
        "label": "Fib pullback (DCA) (ML Gate)",
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


def load_dual_hedge_enabled() -> bool:
    if not DUAL_HEDGE_FILE.is_file():
        return False
    try:
        data = json.loads(DUAL_HEDGE_FILE.read_text(encoding="utf-8"))
        return bool(data.get("enabled", False))
    except (json.JSONDecodeError, OSError):
        return False


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
    use_exit_signal = False
    use_custom_stoploss = False

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
        # Hedge leg always follows the primary — do not re-check ML / cooldown.
        if entry_tag and HEDGE_TAG_SUFFIX in str(entry_tag):
            return True
        if tag in STRATEGY_REGISTRY:
            inst = self._get_instance(tag)
            cooldown = getattr(inst, "pair_in_cooldown", None)
            if callable(cooldown) and cooldown(pair, current_time):
                return False
        scenario = SCENARIO_BY_TAG.get(tag)
        if not scenario:
            return True
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stake = float(self.config.get("stake_amount") or 0)
        return allow_trade_entry(
            scenario=scenario,
            pair=pair,
            rate=rate,
            side=side,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=float(self.stoploss),
            minimal_roi=dict(self.minimal_roi),
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
        side = "short" if trade.is_short else "long"
        ml = pop_entry_ml(pair, side, scenario["scenario_id"])
        save_ml_to_trade(trade, ml)
