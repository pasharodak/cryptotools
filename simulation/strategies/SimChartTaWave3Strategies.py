# Fifth wave: more popular TA indicators not yet in the sim.
"""AO, PPO, CMO, TEMA, Chaikin Osc, ROC, Elder Ray, Hull MA, Fisher, ATR breakout."""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class AwesomeOscStrategy(_LiteBase):
    """Awesome Oscillator — zero-line cross (Bill Williams)."""

    stoploss = -0.017
    minimal_roi = {"0": 0.011, "45": 0.0055, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["high"] + dataframe["low"]) / 2.0
        dataframe["ao"] = ta.SMA(mid, timeperiod=5) - ta.SMA(mid, timeperiod=34)
        dataframe["ao_prev"] = dataframe["ao"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = (dataframe["ao_prev"] <= 0) & (dataframe["ao"] > 0)
        dn = (dataframe["ao_prev"] >= 0) & (dataframe["ao"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ao"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["ao"] > 0, "exit_short"] = 1
        return dataframe


class PpoSignalStrategy(_LiteBase):
    """Percentage Price Oscillator — PPO vs signal EMA."""

    stoploss = -0.016
    minimal_roi = {"0": 0.01, "40": 0.005, "120": 0}
    pair_cooldown_minutes = 95

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ppo"] = ta.PPO(dataframe, fastperiod=12, slowperiod=26, matype=1)
        dataframe["ppo_sig"] = ta.EMA(dataframe["ppo"], timeperiod=9)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = qtpylib.crossed_above(dataframe["ppo"], dataframe["ppo_sig"])
        dn = qtpylib.crossed_below(dataframe["ppo"], dataframe["ppo_sig"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["ppo"], dataframe["ppo_sig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["ppo"], dataframe["ppo_sig"]), "exit_short"
        ] = 1
        return dataframe


class CmoReclaimStrategy(_LiteBase):
    """Chande Momentum Oscillator — fade ±50 reclaim."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["cmo"] = ta.CMO(dataframe, timeperiod=14)
        dataframe["cmo_prev"] = dataframe["cmo"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["cmo_prev"] < -50) & (dataframe["cmo"] >= -50)
        reclaim_dn = (dataframe["cmo_prev"] > 50) & (dataframe["cmo"] <= 50)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["cmo"] > 0, "exit_long"] = 1
        dataframe.loc[dataframe["cmo"] < 0, "exit_short"] = 1
        return dataframe


class TemaCrossStrategy(_LiteBase):
    """TEMA fast/slow cross — triple EMA trend."""

    stoploss = -0.018
    minimal_roi = {"0": 0.012, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 110

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["tema_fast"] = ta.TEMA(dataframe, timeperiod=10)
        dataframe["tema_slow"] = ta.TEMA(dataframe, timeperiod=30)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["tema_fast"], dataframe["tema_slow"])
        dn = qtpylib.crossed_below(dataframe["tema_fast"], dataframe["tema_slow"])
        dataframe.loc[up & vol & trend, "enter_long"] = 1
        dataframe.loc[dn & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["tema_fast"], dataframe["tema_slow"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["tema_fast"], dataframe["tema_slow"]), "exit_short"
        ] = 1
        return dataframe


class ChaikinOscStrategy(_LiteBase):
    """Chaikin Oscillator — ADL EMA(3)−EMA(10) zero cross."""

    stoploss = -0.017
    minimal_roi = {"0": 0.011, "45": 0.0055, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adosc"] = ta.ADOSC(dataframe, fastperiod=3, slowperiod=10)
        dataframe["adosc_prev"] = dataframe["adosc"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 16
        up = (dataframe["adosc_prev"] <= 0) & (dataframe["adosc"] > 0)
        dn = (dataframe["adosc_prev"] >= 0) & (dataframe["adosc"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["adosc"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["adosc"] > 0, "exit_short"] = 1
        return dataframe


class RocMomentumStrategy(_LiteBase):
    """Rate of Change — ROC zero cross with EMA filter."""

    stoploss = -0.016
    minimal_roi = {"0": 0.01, "40": 0.005, "120": 0}
    pair_cooldown_minutes = 95

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=12)
        dataframe["roc_prev"] = dataframe["roc"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = (dataframe["roc_prev"] <= 0) & (dataframe["roc"] > 0)
        dn = (dataframe["roc_prev"] >= 0) & (dataframe["roc"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["roc"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["roc"] > 0, "exit_short"] = 1
        return dataframe


class ElderRayStrategy(_LiteBase):
    """Elder Ray — Bull/Bear Power flip around zero."""

    stoploss = -0.017
    minimal_roi = {"0": 0.011, "45": 0.0055, "130": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ema = ta.EMA(dataframe, timeperiod=13)
        dataframe["ema13"] = ema
        dataframe["bull_power"] = dataframe["high"] - ema
        dataframe["bear_power"] = dataframe["low"] - ema
        dataframe["bull_prev"] = dataframe["bull_power"].shift(1)
        dataframe["bear_prev"] = dataframe["bear_power"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        # long: bear power reclaim above 0 while bull > 0; short inverse
        long_sig = (
            (dataframe["bear_prev"] < 0)
            & (dataframe["bear_power"] >= 0)
            & (dataframe["bull_power"] > 0)
        )
        short_sig = (
            (dataframe["bull_prev"] > 0)
            & (dataframe["bull_power"] <= 0)
            & (dataframe["bear_power"] < 0)
        )
        dataframe.loc[long_sig & vol & trend, "enter_long"] = 1
        dataframe.loc[short_sig & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["bear_power"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["bull_power"] > 0, "exit_short"] = 1
        return dataframe


class HullMaCrossStrategy(_LiteBase):
    """Hull Moving Average — price cross of HMA(20)."""

    stoploss = -0.018
    minimal_roi = {"0": 0.012, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 110

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        period = 20
        half = max(period // 2, 1)
        sqrt_n = max(int(np.sqrt(period)), 1)
        wma_half = ta.WMA(dataframe, timeperiod=half)
        wma_full = ta.WMA(dataframe, timeperiod=period)
        raw = 2 * wma_half - wma_full
        dataframe["hma"] = ta.WMA(raw, timeperiod=sqrt_n)
        dataframe["hma_slope"] = dataframe["hma"] - dataframe["hma"].shift(2)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 17
        up = qtpylib.crossed_above(dataframe["close"], dataframe["hma"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["hma"])
        dataframe.loc[up & vol & trend & (dataframe["hma_slope"] > 0), "enter_long"] = 1
        dataframe.loc[dn & vol & trend & (dataframe["hma_slope"] < 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["hma"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["hma"], "exit_short"] = 1
        return dataframe


class FisherTransformStrategy(_LiteBase):
    """Ehlers Fisher Transform — Fisher / Trigger cross."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        period = 10
        mid = (dataframe["high"] + dataframe["low"]) / 2.0
        lo = mid.rolling(period).min()
        hi = mid.rolling(period).max()
        span = (hi - lo).replace(0, np.nan)
        value = 0.33 * 2 * ((mid - lo) / span - 0.5)
        value = value.clip(-0.999, 0.999).fillna(0.0)
        fish = np.zeros(len(dataframe))
        vals = value.to_numpy()
        for i in range(1, len(dataframe)):
            fish[i] = 0.5 * np.log((1 + vals[i]) / (1 - vals[i]) + 1e-12) + 0.5 * fish[i - 1]
        dataframe["fisher"] = fish
        dataframe["fisher_trig"] = dataframe["fisher"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        chop = dataframe["adx"] < 30
        up = qtpylib.crossed_above(dataframe["fisher"], dataframe["fisher_trig"])
        dn = qtpylib.crossed_below(dataframe["fisher"], dataframe["fisher_trig"])
        dataframe.loc[up & (dataframe["fisher"] < -0.5) & chop & vol, "enter_long"] = 1
        dataframe.loc[dn & (dataframe["fisher"] > 0.5) & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["fisher"], dataframe["fisher_trig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["fisher"], dataframe["fisher_trig"]), "exit_short"
        ] = 1
        return dataframe


class AtrChannelBreakoutStrategy(_LiteBase):
    """ATR channel breakout — close beyond EMA ± k·ATR."""

    stoploss = -0.02
    minimal_roi = {"0": 0.014, "60": 0.007, "180": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        k = 1.8
        dataframe["atr_upper"] = dataframe["ema20"] + k * dataframe["atr"]
        dataframe["atr_lower"] = dataframe["ema20"] - k * dataframe["atr"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["close"], dataframe["atr_upper"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["atr_lower"])
        dataframe.loc[up & vol & trend, "enter_long"] = 1
        dataframe.loc[dn & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema20"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema20"], "exit_short"] = 1
        return dataframe


class AtrChannelBreakoutTestStrategy(AtrChannelBreakoutStrategy):
    """ATR-channel test: no chase entries; exit when close loses EMA20 (via router custom_exit)."""

    pair_cooldown_minutes = 180
    rsi_long_max = 65
    rsi_short_min = 35
    range_lookback = 12
    max_long_range_pos = 0.75
    min_short_range_pos = 0.25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback).max()
        lo = dataframe["low"].rolling(self.range_lookback).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        dataframe.loc[late_long, "enter_long"] = 0
        dataframe.loc[late_short, "enter_short"] = 0
        return dataframe

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        ema20 = last.get("ema20")
        close = float(last.get("close") or current_rate or 0)
        if ema20 is None or close <= 0:
            return None
        ema20 = float(ema20)
        if trade.is_short and close > ema20:
            return "atr_fail"
        if (not trade.is_short) and close < ema20:
            return "atr_fail"
        return None


class ChaikinOscTestStrategy(ChaikinOscStrategy):
    """UI test clone of #5: 1x, SL -3%, no RSI/range chase, custom exit."""

    stoploss = -0.03
    minimal_roi = {'0': 0.012, '60': 0.008, '180': 0.005, '480': 0.0}
    pair_cooldown_minutes = 180
    sim_leverage = 1.0
    rsi_long_max = 65
    rsi_short_min = 35
    range_lookback_1h = 12
    max_long_range_pos = 0.75
    min_short_range_pos = 0.25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        dataframe.loc[late_long, "enter_long"] = 0
        dataframe.loc[late_short, "enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        v = last.get("adosc")
        if v is None:
            return None
        v = float(v)
        if trade.is_short and v > 0:
            return "adosc_flip"
        if (not trade.is_short) and v < 0:
            return "adosc_flip"
        return None


class PpoSignalTestStrategy(PpoSignalStrategy):
    """UI test clone of #7: 1x, SL -3%, no RSI/range chase, custom exit."""

    stoploss = -0.03
    minimal_roi = {'0': 0.012, '60': 0.008, '180': 0.005, '480': 0.0}
    pair_cooldown_minutes = 180
    sim_leverage = 1.0
    rsi_long_max = 65
    rsi_short_min = 35
    range_lookback_1h = 12
    max_long_range_pos = 0.75
    min_short_range_pos = 0.25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        dataframe.loc[late_long, "enter_long"] = 0
        dataframe.loc[late_short, "enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        ppo = last.get("ppo")
        sig = last.get("ppo_sig")
        if ppo is None or sig is None:
            return None
        ppo, sig = float(ppo), float(sig)
        if trade.is_short and ppo > sig:
            return "ppo_flip"
        if (not trade.is_short) and ppo < sig:
            return "ppo_flip"
        return None


class ElderRayTestStrategy(ElderRayStrategy):
    """UI test clone of #10: 1x, SL -3%, no RSI/range chase, custom exit."""

    stoploss = -0.03
    minimal_roi = {'0': 0.012, '60': 0.008, '180': 0.005, '480': 0.0}
    pair_cooldown_minutes = 180
    sim_leverage = 1.0
    rsi_long_max = 65
    rsi_short_min = 35
    range_lookback_1h = 12
    max_long_range_pos = 0.75
    min_short_range_pos = 0.25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        dataframe.loc[late_long, "enter_long"] = 0
        dataframe.loc[late_short, "enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        bear = last.get("bear_power")
        bull = last.get("bull_power")
        if bear is None or bull is None:
            return None
        bear, bull = float(bear), float(bull)
        if trade.is_short and bull > 0:
            return "elder_flip"
        if (not trade.is_short) and bear < 0:
            return "elder_flip"
        return None
