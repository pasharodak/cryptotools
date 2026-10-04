# Second wave of popular strategies not previously in the sim.
"""Keltner, Donchian/Turtle, CCI, Ichimoku TK-cross, Parabolic SAR."""

from __future__ import annotations

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class KeltnerBreakoutStrategy(_LiteBase):
    """Keltner Channel breakout — close outside EMA±ATR band with volume."""

    stoploss = -0.02
    minimal_roi = {"0": 0.015, "60": 0.008, "180": 0}
    pair_cooldown_minutes = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["kc_upper"] = dataframe["ema20"] + 1.5 * dataframe["atr"]
        dataframe["kc_lower"] = dataframe["ema20"] - 1.5 * dataframe["atr"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["close"], dataframe["kc_upper"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["kc_lower"])
        dataframe.loc[up & vol & trend, "enter_long"] = 1
        dataframe.loc[dn & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema20"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema20"], "exit_short"] = 1
        return dataframe


class DonchianBreakoutStrategy(_LiteBase):
    """Donchian / Turtle-style — break of 20-bar high/low."""

    stoploss = -0.025
    minimal_roi = {"0": 0.018, "90": 0.009, "240": 0}
    pair_cooldown_minutes = 180
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["don_high"] = dataframe["high"].rolling(20).max().shift(1)
        dataframe["don_low"] = dataframe["low"].rolling(20).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        break_up = dataframe["close"] > dataframe["don_high"]
        break_dn = dataframe["close"] < dataframe["don_low"]
        # fresh break: previous bar was inside channel
        fresh_up = break_up & (dataframe["close"].shift(1) <= dataframe["don_high"])
        fresh_dn = break_dn & (dataframe["close"].shift(1) >= dataframe["don_low"])
        dataframe.loc[fresh_up & vol & (dataframe["close"] > dataframe["ema50"]), "enter_long"] = 1
        dataframe.loc[fresh_dn & vol & (dataframe["close"] < dataframe["ema50"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["don_high"] + dataframe["don_low"]) / 2
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class CciReversalStrategy(_LiteBase):
    """CCI extremes — fade CCI ±100 reclaim (classic mean-reversion)."""

    stoploss = -0.018
    minimal_roi = {"0": 0.012, "45": 0.006, "120": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["cci"] = ta.CCI(dataframe, timeperiod=20)
        dataframe["cci_prev"] = dataframe["cci"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["cci_prev"] < -100) & (dataframe["cci"] >= -100)
        reclaim_dn = (dataframe["cci_prev"] > 100) & (dataframe["cci"] <= 100)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["cci"] > 0, "exit_long"] = 1
        dataframe.loc[dataframe["cci"] < 0, "exit_short"] = 1
        return dataframe


class IchimokuTkCrossStrategy(_LiteBase):
    """Ichimoku TK cross — Tenkan/Kijun cross with price vs cloud bias."""

    stoploss = -0.022
    minimal_roi = {"0": 0.016, "80": 0.008, "200": 0}
    pair_cooldown_minutes = 150
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        high9 = dataframe["high"].rolling(9).max()
        low9 = dataframe["low"].rolling(9).min()
        high26 = dataframe["high"].rolling(26).max()
        low26 = dataframe["low"].rolling(26).min()
        high52 = dataframe["high"].rolling(52).max()
        low52 = dataframe["low"].rolling(52).min()
        dataframe["tenkan"] = (high9 + low9) / 2
        dataframe["kijun"] = (high26 + low26) / 2
        dataframe["senkou_a"] = ((dataframe["tenkan"] + dataframe["kijun"]) / 2).shift(26)
        dataframe["senkou_b"] = ((high52 + low52) / 2).shift(26)
        dataframe["cloud_top"] = dataframe[["senkou_a", "senkou_b"]].max(axis=1)
        dataframe["cloud_bot"] = dataframe[["senkou_a", "senkou_b"]].min(axis=1)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        tk_up = qtpylib.crossed_above(dataframe["tenkan"], dataframe["kijun"])
        tk_dn = qtpylib.crossed_below(dataframe["tenkan"], dataframe["kijun"])
        above = dataframe["close"] > dataframe["cloud_top"]
        below = dataframe["close"] < dataframe["cloud_bot"]
        dataframe.loc[tk_up & above & vol, "enter_long"] = 1
        dataframe.loc[tk_dn & below & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["tenkan"], dataframe["kijun"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["tenkan"], dataframe["kijun"]), "exit_short"] = 1
        return dataframe


class PsaraFlipStrategy(_LiteBase):
    """Parabolic SAR flip — enter on SAR side change with EMA filter."""

    # Live 3d: avg win ~0.9% / avg loss ~2% — raise TP, slow ROI decay (SL kept).
    stoploss = -0.02
    minimal_roi = {"0": 0.022, "120": 0.012, "360": 0.006, "720": 0}
    pair_cooldown_minutes = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sar"] = ta.SAR(dataframe, acceleration=0.02, maximum=0.2)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["above_sar"] = dataframe["close"] > dataframe["sar"]
        dataframe["above_sar_prev"] = dataframe["above_sar"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 16
        flip_up = dataframe["above_sar"] & (~dataframe["above_sar_prev"].fillna(False))
        flip_dn = (~dataframe["above_sar"]) & dataframe["above_sar_prev"].fillna(False)
        dataframe.loc[flip_up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"] = 1
        dataframe.loc[flip_dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        flip_dn = (~dataframe["above_sar"]) & dataframe["above_sar_prev"].fillna(False)
        flip_up = dataframe["above_sar"] & (~dataframe["above_sar_prev"].fillna(False))
        dataframe.loc[flip_dn, "exit_long"] = 1
        dataframe.loc[flip_up, "exit_short"] = 1
        return dataframe


class PsaraFlipTestStrategy(PsaraFlipStrategy):
    """PSAR test: long-only, tighter chase (simeon shorts + late longs still bleed)."""

    pair_cooldown_minutes = 360
    rsi_long_max = 55
    rsi_short_min = 45
    range_lookback = 12
    max_long_range_pos = 0.55
    min_short_range_pos = 0.45
    allow_short_entries = False
    stoploss = -0.02
    minimal_roi = {"0": 0.02, "90": 0.012, "240": 0.006, "480": 0}
    min_adx = 20.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
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
        weak = dataframe["adx"] < self.min_adx
        dataframe.loc[late_long | weak, "enter_long"] = 0
        dataframe.loc[late_short | weak, "enter_short"] = 0
        if not self.allow_short_entries:
            dataframe["enter_short"] = 0
        return dataframe

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        above = bool(last.get("above_sar"))
        if trade.is_short and above:
            return "sar_flip"
        if (not trade.is_short) and (not above):
            return "sar_flip"
        return None


class DonchianBreakoutTestStrategy(DonchianBreakoutStrategy):
    """UI test clone of #6: 1x, SL -3%, no RSI/range chase, custom exit."""

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
        hi = last.get("don_high")
        lo = last.get("don_low")
        close = float(last.get("close") or current_rate or 0)
        if hi is None or lo is None or close <= 0:
            return None
        mid = (float(hi) + float(lo)) / 2.0
        if trade.is_short and close > mid:
            return "don_mid"
        if (not trade.is_short) and close < mid:
            return "don_mid"
        return None
