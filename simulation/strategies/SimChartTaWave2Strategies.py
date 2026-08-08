# Fourth wave: more classic chart/TA indicators not yet in the sim.
"""Aroon, MFI, StochRSI, TRIX, Ultimate Osc, OBV, Engulfing, Vortex, CMF, KAMA."""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class AroonCrossStrategy(_LiteBase):
    """Aroon Up/Down cross — trend inception signal."""

    stoploss = -0.018
    minimal_roi = {"0": 0.012, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        aroon = ta.AROON(dataframe, timeperiod=25)
        dataframe["aroon_up"] = aroon["aroonup"]
        dataframe["aroon_down"] = aroon["aroondown"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["aroon_up"], dataframe["aroon_down"])
        dn = qtpylib.crossed_below(dataframe["aroon_up"], dataframe["aroon_down"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["aroon_up"], dataframe["aroon_down"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["aroon_up"], dataframe["aroon_down"]), "exit_short"
        ] = 1
        return dataframe


class MfiReclaimStrategy(_LiteBase):
    """Money Flow Index — fade MFI extremes reclaiming 20 / 80."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["mfi"] = ta.MFI(dataframe, timeperiod=14)
        dataframe["mfi_prev"] = dataframe["mfi"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["mfi_prev"] < 20) & (dataframe["mfi"] >= 20)
        reclaim_dn = (dataframe["mfi_prev"] > 80) & (dataframe["mfi"] <= 80)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["mfi"] > 50, "exit_long"] = 1
        dataframe.loc[dataframe["mfi"] < 50, "exit_short"] = 1
        return dataframe


class StochRsiCrossStrategy(_LiteBase):
    """Stochastic RSI — %K/%D cross in oversold/overbought zones."""

    stoploss = -0.014
    minimal_roi = {"0": 0.009, "35": 0.0045, "90": 0}
    pair_cooldown_minutes = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        st = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=5, fastd_period=3, fastd_matype=0)
        dataframe["stochrsi_k"] = st["fastk"]
        dataframe["stochrsi_d"] = st["fastd"]
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        chop = dataframe["adx"] < 30
        k_up = qtpylib.crossed_above(dataframe["stochrsi_k"], dataframe["stochrsi_d"])
        k_dn = qtpylib.crossed_below(dataframe["stochrsi_k"], dataframe["stochrsi_d"])
        dataframe.loc[k_up & (dataframe["stochrsi_k"] < 25) & chop & vol, "enter_long"] = 1
        dataframe.loc[k_dn & (dataframe["stochrsi_k"] > 75) & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["stochrsi_k"], dataframe["stochrsi_d"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["stochrsi_k"], dataframe["stochrsi_d"]), "exit_short"
        ] = 1
        return dataframe


class TrixSignalStrategy(_LiteBase):
    """TRIX — oscillator vs its signal EMA."""

    stoploss = -0.017
    minimal_roi = {"0": 0.011, "45": 0.0055, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["trix"] = ta.TRIX(dataframe, timeperiod=15)
        dataframe["trix_sig"] = ta.EMA(dataframe["trix"], timeperiod=9)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = qtpylib.crossed_above(dataframe["trix"], dataframe["trix_sig"])
        dn = qtpylib.crossed_below(dataframe["trix"], dataframe["trix_sig"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["trix"], dataframe["trix_sig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["trix"], dataframe["trix_sig"]), "exit_short"
        ] = 1
        return dataframe


class UltimateOscStrategy(_LiteBase):
    """Ultimate Oscillator — reclaim 30 / 70 levels."""

    stoploss = -0.016
    minimal_roi = {"0": 0.01, "40": 0.005, "120": 0}
    pair_cooldown_minutes = 95

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["uo"] = ta.ULTOSC(dataframe, timeperiod1=7, timeperiod2=14, timeperiod3=28)
        dataframe["uo_prev"] = dataframe["uo"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["uo_prev"] < 30) & (dataframe["uo"] >= 30)
        reclaim_dn = (dataframe["uo_prev"] > 70) & (dataframe["uo"] <= 70)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["uo"] > 50, "exit_long"] = 1
        dataframe.loc[dataframe["uo"] < 50, "exit_short"] = 1
        return dataframe


class ObvEmaCrossStrategy(_LiteBase):
    """OBV vs its EMA — volume-trend confirmation."""

    stoploss = -0.018
    minimal_roi = {"0": 0.012, "55": 0.006, "160": 0}
    pair_cooldown_minutes = 110

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["obv"] = ta.OBV(dataframe)
        dataframe["obv_ema"] = ta.EMA(dataframe["obv"], timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 17
        up = qtpylib.crossed_above(dataframe["obv"], dataframe["obv_ema"])
        dn = qtpylib.crossed_below(dataframe["obv"], dataframe["obv_ema"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["obv"], dataframe["obv_ema"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["obv"], dataframe["obv_ema"]), "exit_short"] = 1
        return dataframe


class EngulfingTrendStrategy(_LiteBase):
    """Bullish/bearish engulfing candle with EMA trend filter."""

    stoploss = -0.016
    minimal_roi = {"0": 0.011, "40": 0.0055, "110": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["engulf"] = ta.CDLENGULFING(dataframe)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 15
        bull = dataframe["engulf"] > 0
        bear = dataframe["engulf"] < 0
        dataframe.loc[
            bull & vol & trend & (dataframe["close"] > dataframe["ema50"]) & (dataframe["rsi"] < 60),
            "enter_long",
        ] = 1
        dataframe.loc[
            bear & vol & trend & (dataframe["close"] < dataframe["ema50"]) & (dataframe["rsi"] > 40),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["engulf"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["engulf"] > 0, "exit_short"] = 1
        return dataframe


class VortexCrossStrategy(_LiteBase):
    """Vortex Indicator — VI+ / VI− cross (trend confirmation)."""

    stoploss = -0.019
    minimal_roi = {"0": 0.013, "55": 0.0065, "160": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        period = 14
        tr = ta.TRANGE(dataframe)
        vm_plus = (dataframe["high"] - dataframe["low"].shift(1)).abs()
        vm_minus = (dataframe["low"] - dataframe["high"].shift(1)).abs()
        dataframe["vi_plus"] = vm_plus.rolling(period).sum() / tr.rolling(period).sum().replace(0, np.nan)
        dataframe["vi_minus"] = vm_minus.rolling(period).sum() / tr.rolling(period).sum().replace(0, np.nan)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["vi_plus"], dataframe["vi_minus"])
        dn = qtpylib.crossed_below(dataframe["vi_plus"], dataframe["vi_minus"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["vi_plus"], dataframe["vi_minus"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["vi_plus"], dataframe["vi_minus"]), "exit_short"
        ] = 1
        return dataframe


class CmfZeroCrossStrategy(_LiteBase):
    """Chaikin Money Flow — zero-line cross with volume."""

    stoploss = -0.017
    minimal_roi = {"0": 0.011, "45": 0.0055, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hl = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        mfm = ((dataframe["close"] - dataframe["low"]) - (dataframe["high"] - dataframe["close"])) / hl
        mfv = mfm.fillna(0) * dataframe["volume"]
        vol_sum = dataframe["volume"].rolling(20).sum().replace(0, np.nan)
        dataframe["cmf"] = mfv.rolling(20).sum() / vol_sum
        dataframe["cmf_prev"] = dataframe["cmf"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 16
        up = (dataframe["cmf_prev"] <= 0) & (dataframe["cmf"] > 0)
        dn = (dataframe["cmf_prev"] >= 0) & (dataframe["cmf"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["cmf"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["cmf"] > 0, "exit_short"] = 1
        return dataframe


class KamaTrendStrategy(_LiteBase):
    """Kaufman Adaptive MA — price cross with slope filter."""

    stoploss = -0.02
    minimal_roi = {"0": 0.014, "60": 0.007, "180": 0}
    pair_cooldown_minutes = 130

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["kama"] = ta.KAMA(dataframe, timeperiod=30)
        dataframe["kama_slope"] = dataframe["kama"] - dataframe["kama"].shift(3)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["close"], dataframe["kama"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["kama"])
        dataframe.loc[up & vol & trend & (dataframe["kama_slope"] > 0), "enter_long"] = 1
        dataframe.loc[dn & vol & trend & (dataframe["kama_slope"] < 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["kama"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["kama"], "exit_short"] = 1
        return dataframe
