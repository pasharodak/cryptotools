# pragma pylint: disable=missing-docstring, invalid-name
"""Triple EMA — вход по стеку EMA 8/21/55 (тренд-следование)."""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class TripleEmaStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 60

    minimal_roi = {"0": 0.03, "60": 0.015, "120": 0.005, "240": 0}
    stoploss = -0.05
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["ema_mid"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=55)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bull_stack = (
            (dataframe["ema_fast"] > dataframe["ema_mid"])
            & (dataframe["ema_mid"] > dataframe["ema_slow"])
        )
        bear_stack = (
            (dataframe["ema_fast"] < dataframe["ema_mid"])
            & (dataframe["ema_mid"] < dataframe["ema_slow"])
        )

        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["ema_fast"], dataframe["ema_mid"])
                & bull_stack
                & (dataframe["rsi"] > 50)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe["ema_fast"], dataframe["ema_mid"])
                & bear_stack
                & (dataframe["rsi"] < 50)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema_fast"] < dataframe["ema_mid"])
                | (dataframe["rsi"] > 78)
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["ema_fast"] > dataframe["ema_mid"])
                | (dataframe["rsi"] < 22)
            ),
            "exit_short",
        ] = 1

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
