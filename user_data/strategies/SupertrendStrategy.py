# pragma pylint: disable=missing-docstring, invalid-name
"""Supertrend — популярный трендовый индикатор на ATR (long/short)."""

import numpy as np
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy


class SupertrendStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    minimal_roi = {"0": 0.03, "60": 0.015, "120": 0.005, "240": 0}
    stoploss = -0.05
    trailing_stop = False

    supertrend_period = 10
    supertrend_multiplier = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr = ta.ATR(dataframe, timeperiod=self.supertrend_period)
        hl2 = (dataframe["high"] + dataframe["low"]) / 2
        upper = hl2 + (self.supertrend_multiplier * atr)
        lower = hl2 - (self.supertrend_multiplier * atr)

        uptrend = np.ones(len(dataframe), dtype=bool)
        st = lower.copy()

        for i in range(1, len(dataframe)):
            if dataframe["close"].iloc[i] > upper.iloc[i - 1]:
                uptrend[i] = True
            elif dataframe["close"].iloc[i] < lower.iloc[i - 1]:
                uptrend[i] = False
            else:
                uptrend[i] = uptrend[i - 1]
                if uptrend[i] and lower.iloc[i] < lower.iloc[i - 1]:
                    lower.iloc[i] = lower.iloc[i - 1]
                if not uptrend[i] and upper.iloc[i] > upper.iloc[i - 1]:
                    upper.iloc[i] = upper.iloc[i - 1]
            st.iloc[i] = lower.iloc[i] if uptrend[i] else upper.iloc[i]

        dataframe["supertrend"] = st
        dataframe["supertrend_up"] = uptrend
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(
            False
        )
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(
            False
        )

        dataframe.loc[
            (up_flip & (dataframe["rsi"] > 45) & (dataframe["volume"] > 0)),
            "enter_long",
        ] = 1

        dataframe.loc[
            (down_flip & (dataframe["rsi"] < 55) & (dataframe["volume"] > 0)),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(
            False
        )
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(
            False
        )

        dataframe.loc[(down_flip), "exit_long"] = 1
        dataframe.loc[(up_flip), "exit_short"] = 1

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
