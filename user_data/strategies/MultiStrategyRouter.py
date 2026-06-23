# pragma pylint: disable=missing-docstring, invalid-name
"""Combines signals from enabled sub-strategies (see user_data/enabled_strategies.json)."""

from __future__ import annotations

import json
from pathlib import Path

from pandas import DataFrame

from freqtrade.strategy import IStrategy

from AdxMomentumStrategy import AdxMomentumStrategy
from BollingerRsiStrategy import BollingerRsiStrategy
from CriptoPairsStrategy import CriptoPairsStrategy
from MacdEmaStrategy import MacdEmaStrategy
from SupertrendStrategy import SupertrendStrategy
from TripleEmaStrategy import TripleEmaStrategy

ENABLED_FILE = Path(__file__).resolve().parent.parent / "enabled_strategies.json"

STRATEGY_REGISTRY: dict[str, type[IStrategy]] = {
    "CriptoPairsStrategy": CriptoPairsStrategy,
    "SupertrendStrategy": SupertrendStrategy,
    "MacdEmaStrategy": MacdEmaStrategy,
    "TripleEmaStrategy": TripleEmaStrategy,
    "BollingerRsiStrategy": BollingerRsiStrategy,
    "AdxMomentumStrategy": AdxMomentumStrategy,
}


def load_enabled_map() -> dict[str, bool]:
    if not ENABLED_FILE.is_file():
        return {sid: sid == "CriptoPairsStrategy" for sid in STRATEGY_REGISTRY}
    data = json.loads(ENABLED_FILE.read_text(encoding="utf-8"))
    enabled = data.get("enabled", {})
    return {sid: bool(enabled.get(sid, False)) for sid in STRATEGY_REGISTRY}


class MultiStrategyRouter(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 220

    minimal_roi = {
        "0": 0.03,
        "60": 0.015,
        "120": 0.005,
        "240": 0,
    }
    stoploss = -0.05
    trailing_stop = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._instances: dict[str, IStrategy] = {}

    def _enabled_ids(self) -> list[str]:
        enabled = load_enabled_map()
        return [sid for sid, on in enabled.items() if on]

    def _get_instance(self, strategy_id: str) -> IStrategy:
        if strategy_id not in self._instances:
            self._instances[strategy_id] = STRATEGY_REGISTRY[strategy_id](self.config)
        return self._instances[strategy_id]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        for sid in self._enabled_ids():
            strat = self._get_instance(sid)
            df = strat.populate_indicators(dataframe.copy(), metadata)
            df = strat.populate_entry_trend(df, metadata)

            long_mask = df.get("enter_long", 0).fillna(0).astype(int) == 1
            short_mask = df.get("enter_short", 0).fillna(0).astype(int) == 1

            first_long = long_mask & (dataframe["enter_long"] != 1)
            first_short = short_mask & (dataframe["enter_short"] != 1)
            dataframe.loc[first_long, "enter_tag"] = sid
            dataframe.loc[first_short, "enter_tag"] = sid
            dataframe.loc[long_mask, "enter_long"] = 1
            dataframe.loc[short_mask, "enter_short"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0

        for sid in self._enabled_ids():
            strat = self._get_instance(sid)
            df = strat.populate_indicators(dataframe.copy(), metadata)
            df = strat.populate_exit_trend(df, metadata)

            long_mask = df.get("exit_long", 0).fillna(0).astype(int) == 1
            short_mask = df.get("exit_short", 0).fillna(0).astype(int) == 1
            dataframe.loc[long_mask, "exit_long"] = 1
            dataframe.loc[short_mask, "exit_short"] = 1

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
        return min(3.0, max_leverage)
