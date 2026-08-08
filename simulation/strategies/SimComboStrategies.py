# Combo strategies from TA literature: trend + momentum + volume/volatility.
"""Inspired by Investopedia/Excavo triad and expectancy (trend vs mean-rev profiles)."""

from __future__ import annotations

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class EmaRsiAtrComboStrategy(_LiteBase):
    """EMA trend + RSI pullback entry + ATR regime (classic 3-question kit)."""

    # Trend profile: slightly wider TP vs SL (expectancy via R:R)
    stoploss = -0.018
    minimal_roi = {"0": 0.014, "50": 0.007, "150": 0}
    pair_cooldown_minutes = 110

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_sma"] = dataframe["atr"].rolling(20).mean()
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        # volatility not dead; trend present
        atr_ok = dataframe["atr"] > dataframe["atr_sma"] * 0.7
        trend = dataframe["adx"] > 18
        up_trend = dataframe["ema20"] > dataframe["ema50"]
        dn_trend = dataframe["ema20"] < dataframe["ema50"]
        # pullback timing
        long_rsi = (dataframe["rsi"] > 40) & (dataframe["rsi"] < 55) & (
            dataframe["rsi"] > dataframe["rsi"].shift(1)
        )
        short_rsi = (dataframe["rsi"] < 60) & (dataframe["rsi"] > 45) & (
            dataframe["rsi"] < dataframe["rsi"].shift(1)
        )
        dataframe.loc[up_trend & long_rsi & trend & vol & atr_ok, "enter_long"] = 1
        dataframe.loc[dn_trend & short_rsi & trend & vol & atr_ok, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ema20"] < dataframe["ema50"], "exit_long"] = 1
        dataframe.loc[dataframe["ema20"] > dataframe["ema50"], "exit_short"] = 1
        return dataframe


class AdxMacdVolComboStrategy(_LiteBase):
    """ADX regime + MACD hist flip + volume confirmation (trend family)."""

    stoploss = -0.02
    minimal_roi = {"0": 0.016, "60": 0.008, "180": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["macdhist_prev"] = dataframe["macdhist"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        strong = dataframe["adx"] > 22
        hist_up = (dataframe["macdhist_prev"] <= 0) & (dataframe["macdhist"] > 0)
        hist_dn = (dataframe["macdhist_prev"] >= 0) & (dataframe["macdhist"] < 0)
        dataframe.loc[
            hist_up & strong & vol & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            hist_dn & strong & vol & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["macdhist"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["macdhist"] > 0, "exit_short"] = 1
        return dataframe


class BbRsiAdxMeanRevComboStrategy(_LiteBase):
    """BB fade + RSI extreme + ADX chop filter (mean-rev only in range regime)."""

    # Mean-rev profile: tighter TP, need high WR
    stoploss = -0.014
    minimal_roi = {"0": 0.009, "35": 0.0045, "90": 0}
    pair_cooldown_minutes = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bb = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]
        dataframe["bb_upper"] = bb["upper"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 22
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        long_sig = (
            (dataframe["close"] < dataframe["bb_lower"])
            & (dataframe["rsi"] < 32)
            & (dataframe["close"] > dataframe["close"].shift(1))
        )
        short_sig = (
            (dataframe["close"] > dataframe["bb_upper"])
            & (dataframe["rsi"] > 68)
            & (dataframe["close"] < dataframe["close"].shift(1))
        )
        dataframe.loc[long_sig & chop & vol, "enter_long"] = 1
        dataframe.loc[short_sig & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["bb_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["bb_mid"], "exit_short"] = 1
        return dataframe


class SupertrendRsiObvComboStrategy(_LiteBase):
    """Supertrend direction + RSI not extreme + OBV slope (trend + volume)."""

    stoploss = -0.019
    minimal_roi = {"0": 0.015, "55": 0.0075, "160": 0}
    pair_cooldown_minutes = 115
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr = ta.ATR(dataframe, timeperiod=10)
        hl2 = (dataframe["high"] + dataframe["low"]) / 2.0
        mult = 3.0
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        st = [0.0] * len(dataframe)
        direction = [1] * len(dataframe)
        closes = dataframe["close"].to_numpy()
        up = upper.to_numpy()
        lo = lower.to_numpy()
        for i in range(1, len(dataframe)):
            if closes[i - 1] > st[i - 1]:
                st[i] = max(lo[i], st[i - 1])
            else:
                st[i] = min(up[i], st[i - 1])
            if closes[i] > st[i]:
                direction[i] = 1
            elif closes[i] < st[i]:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]
        dataframe["st_line"] = st
        dataframe["st_dir"] = direction
        dataframe["st_dir_prev"] = dataframe["st_dir"].shift(1)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["obv"] = ta.OBV(dataframe)
        dataframe["obv_ema"] = ta.EMA(dataframe["obv"], timeperiod=21)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        flip_up = (dataframe["st_dir"] == 1) & (dataframe["st_dir_prev"] != 1)
        flip_dn = (dataframe["st_dir"] == -1) & (dataframe["st_dir_prev"] != -1)
        rsi_ok_long = (dataframe["rsi"] > 45) & (dataframe["rsi"] < 70)
        rsi_ok_short = (dataframe["rsi"] < 55) & (dataframe["rsi"] > 30)
        obv_up = dataframe["obv"] > dataframe["obv_ema"]
        obv_dn = dataframe["obv"] < dataframe["obv_ema"]
        dataframe.loc[flip_up & rsi_ok_long & obv_up & vol, "enter_long"] = 1
        dataframe.loc[flip_dn & rsi_ok_short & obv_dn & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["st_dir"] == -1, "exit_long"] = 1
        dataframe.loc[dataframe["st_dir"] == 1, "exit_short"] = 1
        return dataframe


class DonchianAdxVolComboStrategy(_LiteBase):
    """Donchian breakout + ADX trend filter + volume spike (breakout family)."""

    stoploss = -0.022
    minimal_roi = {"0": 0.018, "70": 0.009, "200": 0}
    pair_cooldown_minutes = 140
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["don_high"] = dataframe["high"].rolling(20).max().shift(1)
        dataframe["don_low"] = dataframe["low"].rolling(20).min().shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.2
        trend = dataframe["adx"] > 20
        break_up = (dataframe["close"] > dataframe["don_high"]) & (
            dataframe["close"].shift(1) <= dataframe["don_high"]
        )
        break_dn = (dataframe["close"] < dataframe["don_low"]) & (
            dataframe["close"].shift(1) >= dataframe["don_low"]
        )
        dataframe.loc[
            break_up & trend & vol & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            break_dn & trend & vol & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["don_high"] + dataframe["don_low"]) / 2
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class StochCmfEmaComboStrategy(_LiteBase):
    """EMA bias + Stoch timing + CMF money-flow confirm (confluence stack)."""

    stoploss = -0.016
    minimal_roi = {"0": 0.012, "45": 0.006, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        st = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowd_period=3)
        dataframe["slowk"] = st["slowk"]
        dataframe["slowd"] = st["slowd"]
        hl = (dataframe["high"] - dataframe["low"]).replace(0, 1e-12)
        mfm = ((dataframe["close"] - dataframe["low"]) - (dataframe["high"] - dataframe["close"])) / hl
        mfv = mfm * dataframe["volume"]
        dataframe["cmf"] = mfv.rolling(20).sum() / dataframe["volume"].rolling(20).sum().replace(0, 1e-12)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 15
        k_up = qtpylib.crossed_above(dataframe["slowk"], dataframe["slowd"])
        k_dn = qtpylib.crossed_below(dataframe["slowk"], dataframe["slowd"])
        dataframe.loc[
            k_up
            & (dataframe["slowk"] < 30)
            & (dataframe["close"] > dataframe["ema50"])
            & (dataframe["cmf"] > 0)
            & trend
            & vol,
            "enter_long",
        ] = 1
        dataframe.loc[
            k_dn
            & (dataframe["slowk"] > 70)
            & (dataframe["close"] < dataframe["ema50"])
            & (dataframe["cmf"] < 0)
            & trend
            & vol,
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["slowk"], dataframe["slowd"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["slowk"], dataframe["slowd"]), "exit_short"
        ] = 1
        return dataframe


class HmaPpoAtrComboStrategy(_LiteBase):
    """Hull MA trend + PPO momentum cross + ATR expansion filter."""

    stoploss = -0.018
    minimal_roi = {"0": 0.014, "50": 0.007, "150": 0}
    pair_cooldown_minutes = 110

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        period = 20
        half = max(period // 2, 1)
        sqrt_n = max(int(period**0.5), 1)
        wma_half = ta.WMA(dataframe, timeperiod=half)
        wma_full = ta.WMA(dataframe, timeperiod=period)
        raw = 2 * wma_half - wma_full
        dataframe["hma"] = ta.WMA(raw, timeperiod=sqrt_n)
        dataframe["ppo"] = ta.PPO(dataframe, fastperiod=12, slowperiod=26, matype=1)
        dataframe["ppo_sig"] = ta.EMA(dataframe["ppo"], timeperiod=9)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_sma"] = dataframe["atr"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        expand = dataframe["atr"] > dataframe["atr_sma"]
        trend = dataframe["adx"] > 17
        above = dataframe["close"] > dataframe["hma"]
        below = dataframe["close"] < dataframe["hma"]
        ppo_up = qtpylib.crossed_above(dataframe["ppo"], dataframe["ppo_sig"])
        ppo_dn = qtpylib.crossed_below(dataframe["ppo"], dataframe["ppo_sig"])
        dataframe.loc[above & ppo_up & expand & trend & vol, "enter_long"] = 1
        dataframe.loc[below & ppo_dn & expand & trend & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["hma"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["hma"], "exit_short"] = 1
        return dataframe


class KeltnerStochVolComboStrategy(_LiteBase):
    """Keltner breakout + Stoch not overbought/oversold trap + volume."""

    stoploss = -0.02
    minimal_roi = {"0": 0.015, "55": 0.0075, "160": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["kc_upper"] = dataframe["ema20"] + 1.5 * dataframe["atr"]
        dataframe["kc_lower"] = dataframe["ema20"] - 1.5 * dataframe["atr"]
        st = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowd_period=3)
        dataframe["slowk"] = st["slowk"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.15
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["close"], dataframe["kc_upper"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["kc_lower"])
        # avoid chasing already exhausted momentum
        stoch_ok_long = dataframe["slowk"] < 80
        stoch_ok_short = dataframe["slowk"] > 20
        dataframe.loc[up & trend & vol & stoch_ok_long, "enter_long"] = 1
        dataframe.loc[dn & trend & vol & stoch_ok_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema20"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema20"], "exit_short"] = 1
        return dataframe
