# pragma pylint: disable=missing-docstring, invalid-name
"""ADX Momentum — вход при сильном тренде (ADX > 25) и пересечении DI+/DI-."""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class AdxMomentumStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    minimal_roi = {"0": 0.03, "90": 0.015, "180": 0.005, "360": 0}
    stoploss = -0.05
    trailing_stop = False

    adx_threshold = 25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=14)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["adx"] > self.adx_threshold)
                & qtpylib.crossed_above(dataframe["plus_di"], dataframe["minus_di"])
                & (dataframe["close"] > dataframe["ema21"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["adx"] > self.adx_threshold)
                & qtpylib.crossed_above(dataframe["minus_di"], dataframe["plus_di"])
                & (dataframe["close"] < dataframe["ema21"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["adx"] < 20)
                | qtpylib.crossed_below(dataframe["plus_di"], dataframe["minus_di"])
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["adx"] < 20)
                | qtpylib.crossed_below(dataframe["minus_di"], dataframe["plus_di"])
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
