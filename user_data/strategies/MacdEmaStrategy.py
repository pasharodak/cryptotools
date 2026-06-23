# pragma pylint: disable=missing-docstring, invalid-name
"""MACD + EMA200 — классическая стратегия по пересечению MACD в направлении тренда."""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class MacdEmaStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 220

    minimal_roi = {"0": 0.03, "60": 0.015, "120": 0.005, "240": 0}
    stoploss = -0.05
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
                & (dataframe["close"] > dataframe["ema200"])
                & (dataframe["macdhist"] > 0)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["macdhist"] < 0)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
                | (dataframe["rsi"] > 75)
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
                | (dataframe["rsi"] < 25)
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
