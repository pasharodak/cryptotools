"""Classic trend/momentum strategies for sim player (Supertrend, MACD+EMA, RSI+EMA)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase  # noqa: E402


class SimSupertrend(_LiteBase):
    """Supertrend flip + RSI filter (ATR-based trend following)."""

    stoploss = -0.025
    minimal_roi = {"0": 0.03, "120": 0.015, "360": 0.008, "720": 0}
    pair_cooldown_minutes = 360
    startup_candle_count = 80
    sim_leverage = 3.0
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
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(False)
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(False)
        dataframe.loc[up_flip & (dataframe["rsi"] > 45) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[down_flip & (dataframe["rsi"] < 55) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(False)
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(False)
        dataframe.loc[down_flip, "exit_long"] = 1
        dataframe.loc[up_flip, "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimMacdEma(_LiteBase):
    """MACD cross in direction of EMA200 trend."""

    stoploss = -0.025
    minimal_roi = {"0": 0.03, "180": 0.015, "480": 0.008, "960": 0}
    pair_cooldown_minutes = 480
    startup_candle_count = 220
    sim_leverage = 3.0

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
            qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
            & (dataframe["close"] > dataframe["ema200"])
            & (dataframe["macdhist"] > 0)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
            & (dataframe["close"] < dataframe["ema200"])
            & (dataframe["macdhist"] < 0)
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"]) | (dataframe["rsi"] > 75),
            "exit_long",
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"]) | (dataframe["rsi"] < 25),
            "exit_short",
        ] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimRsiEmaCross(_LiteBase):
    """RSI oversold/overbought + EMA12/26 + BB filter (CriptoPairs-style)."""

    stoploss = -0.022
    minimal_roi = {"0": 0.025, "90": 0.012, "240": 0.006, "480": 0}
    pair_cooldown_minutes = 300
    startup_candle_count = 80
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        bb = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_upper"] = bb["upper"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] < 35)
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["rsi"] > 65)
            & (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["close"] < dataframe["bb_upper"])
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > 70) | (dataframe["ema_fast"] < dataframe["ema_slow"]),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["rsi"] < 30) | (dataframe["ema_fast"] > dataframe["ema_slow"]),
            "exit_short",
        ] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)
