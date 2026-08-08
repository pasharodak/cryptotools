# Third wave: classic chart / price-action TA not covered by scalp or newset.
"""Williams %R, ADX DI cross, BB squeeze breakout, Heikin Ashi flip, Pivot bounce."""

from __future__ import annotations

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class WilliamsRReclaimStrategy(_LiteBase):
    """Williams %R — fade oversold/overbought reclaim of −80 / −20."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "100": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["willr"] = ta.WILLR(dataframe, timeperiod=14)
        dataframe["willr_prev"] = dataframe["willr"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 30
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["willr_prev"] < -80) & (dataframe["willr"] >= -80)
        reclaim_dn = (dataframe["willr_prev"] > -20) & (dataframe["willr"] <= -20)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["willr"] > -50, "exit_long"] = 1
        dataframe.loc[dataframe["willr"] < -50, "exit_short"] = 1
        return dataframe


class AdxDiCrossStrategy(_LiteBase):
    """ADX / DI+/DI− — directional index cross with ADX trend filter."""

    stoploss = -0.02
    minimal_roi = {"0": 0.014, "60": 0.007, "180": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 20
        di_up = qtpylib.crossed_above(dataframe["plus_di"], dataframe["minus_di"])
        di_dn = qtpylib.crossed_below(dataframe["plus_di"], dataframe["minus_di"])
        dataframe.loc[
            di_up & trend & vol & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            di_dn & trend & vol & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["plus_di"], dataframe["minus_di"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["plus_di"], dataframe["minus_di"]), "exit_short"
        ] = 1
        return dataframe


class BbSqueezeBreakoutStrategy(_LiteBase):
    """Bollinger squeeze breakout — narrow BB width then close outside bands."""

    stoploss = -0.018
    minimal_roi = {"0": 0.013, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 100
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bb = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]
        dataframe["bb_upper"] = bb["upper"]
        width = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_mid"].replace(0, 1)
        dataframe["bb_width"] = width
        dataframe["bb_width_min"] = width.rolling(40).min()
        dataframe["squeeze"] = width <= dataframe["bb_width_min"] * 1.15
        dataframe["squeeze_prev"] = dataframe["squeeze"].shift(1).fillna(False)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        # squeeze recently active, now expanding via band break
        was_sq = dataframe["squeeze_prev"] | dataframe["squeeze"].shift(2).fillna(False)
        break_up = qtpylib.crossed_above(dataframe["close"], dataframe["bb_upper"])
        break_dn = qtpylib.crossed_below(dataframe["close"], dataframe["bb_lower"])
        trend = dataframe["adx"] > 15
        dataframe.loc[break_up & was_sq & vol & trend, "enter_long"] = 1
        dataframe.loc[break_dn & was_sq & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["bb_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["bb_mid"], "exit_short"] = 1
        return dataframe


class HeikinAshiFlipStrategy(_LiteBase):
    """Heikin Ashi color flip — HA close vs open with EMA trend filter."""

    stoploss = -0.016
    minimal_roi = {"0": 0.011, "45": 0.0055, "120": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ha = qtpylib.heikinashi(dataframe)
        dataframe["ha_open"] = ha["open"]
        dataframe["ha_close"] = ha["close"]
        dataframe["ha_bull"] = dataframe["ha_close"] > dataframe["ha_open"]
        dataframe["ha_bull_prev"] = dataframe["ha_bull"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        flip_up = dataframe["ha_bull"] & (~dataframe["ha_bull_prev"].fillna(False))
        flip_dn = (~dataframe["ha_bull"]) & dataframe["ha_bull_prev"].fillna(False)
        dataframe.loc[
            flip_up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            flip_dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        flip_dn = (~dataframe["ha_bull"]) & dataframe["ha_bull_prev"].fillna(False)
        flip_up = dataframe["ha_bull"] & (~dataframe["ha_bull_prev"].fillna(False))
        dataframe.loc[flip_dn, "exit_long"] = 1
        dataframe.loc[flip_up, "exit_short"] = 1
        return dataframe


class PivotBounceStrategy(_LiteBase):
    """Classic floor pivots — bounce off prior-day S1 / R1 with RSI confirm."""

    stoploss = -0.017
    minimal_roi = {"0": 0.012, "50": 0.006, "140": 0}
    pair_cooldown_minutes = 120
    startup_candle_count = 320  # need ~1 day of 5m bars for pivot lookback

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Rolling "session" approx: prior 288 × 5m ≈ 1 day
        look = 288
        hi = dataframe["high"].rolling(look).max().shift(1)
        lo = dataframe["low"].rolling(look).min().shift(1)
        cl = dataframe["close"].shift(1)
        pp = (hi + lo + cl) / 3.0
        dataframe["pivot"] = pp
        dataframe["r1"] = 2 * pp - lo
        dataframe["s1"] = 2 * pp - hi
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        chop = dataframe["adx"] < 28
        # touch S1 then reclaim above; touch R1 then reject below
        near_s1 = dataframe["low"] <= dataframe["s1"] * 1.001
        near_r1 = dataframe["high"] >= dataframe["r1"] * 0.999
        bounce_up = near_s1 & (dataframe["close"] > dataframe["s1"]) & (dataframe["rsi"] < 45)
        bounce_dn = near_r1 & (dataframe["close"] < dataframe["r1"]) & (dataframe["rsi"] > 55)
        dataframe.loc[bounce_up & vol & chop, "enter_long"] = 1
        dataframe.loc[bounce_dn & vol & chop, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["pivot"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["pivot"], "exit_short"] = 1
        return dataframe
