# pragma pylint: disable=missing-docstring, invalid-name
"""Bollinger + RSI — mean-reversion от полос Боллинджера с фильтром RSI."""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class BollingerRsiStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    minimal_roi = {"0": 0.025, "45": 0.012, "90": 0.005, "180": 0}
    stoploss = -0.05
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bollinger = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bollinger["lower"]
        dataframe["bb_mid"] = bollinger["mid"]
        dataframe["bb_upper"] = bollinger["upper"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] <= dataframe["bb_lower"])
                & (dataframe["rsi"] < 32)
                & (dataframe["close"] > dataframe["ema50"] * 0.97)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["close"] >= dataframe["bb_upper"])
                & (dataframe["rsi"] > 68)
                & (dataframe["close"] < dataframe["ema50"] * 1.03)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] >= dataframe["bb_mid"])
                | (dataframe["rsi"] > 65)
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["close"] <= dataframe["bb_mid"])
                | (dataframe["rsi"] < 35)
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
