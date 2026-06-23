# pragma pylint: disable=missing-docstring, invalid-name
"""Strategy for Bybit USDT perpetual — long/short via RSI + EMA."""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class CriptoPairsStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True

    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    minimal_roi = {
        "0": 0.03,
        "60": 0.015,
        "120": 0.005,
        "240": 0,
    }

    stoploss = -0.05
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        bollinger = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bollinger["lower"]
        dataframe["bb_upper"] = bollinger["upper"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 35)
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["close"] > dataframe["bb_lower"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["rsi"] > 65)
                & (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["close"] < dataframe["bb_upper"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] > 70)
                | (dataframe["ema_fast"] < dataframe["ema_slow"])
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["rsi"] < 30)
                | (dataframe["ema_fast"] > dataframe["ema_slow"])
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
