# Fifth wave: indicators from 2025–2026 crypto TA guides not yet in the sim.
"""STC, QQE, DeMarker, KST, RVI, VWAP, RSI divergence, Chandelier, Z-score, TTM Squeeze."""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from pandas import DataFrame, Series
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


def _stoch_fast(series: Series, period: int) -> Series:
    lo = series.rolling(period).min()
    hi = series.rolling(period).max()
    span = (hi - lo).replace(0, np.nan)
    return 100.0 * (series - lo) / span


class SchaffTrendCycleStrategy(_LiteBase):
    """Schaff Trend Cycle — MACD + double stochastic (LiteFinance STC scalp)."""

    stoploss = -0.016
    minimal_roi = {"0": 0.011, "40": 0.0055, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ema_fast = ta.EMA(dataframe, timeperiod=12)
        ema_slow = ta.EMA(dataframe, timeperiod=26)
        macd = ema_fast - ema_slow
        st1 = _stoch_fast(macd, 10).fillna(50.0)
        # Double smooth like classic STC
        st1_s = st1.ewm(span=3, adjust=False).mean()
        st2 = _stoch_fast(st1_s, 10).fillna(50.0)
        dataframe["stc"] = st2.ewm(span=3, adjust=False).mean()
        dataframe["stc_prev"] = dataframe["stc"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        trend = dataframe["adx"] > 16
        up = (dataframe["stc_prev"] < 25) & (dataframe["stc"] >= 25)
        dn = (dataframe["stc_prev"] > 75) & (dataframe["stc"] <= 75)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["stc"] > 80, "exit_long"] = 1
        dataframe.loc[dataframe["stc"] < 20, "exit_short"] = 1
        return dataframe


class QqeCrossStrategy(_LiteBase):
    """QQE — smoothed RSI vs signal EMA; band filter for volatility-adjusted timing."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rsi = ta.RSI(dataframe, timeperiod=14)
        rsi_ma = rsi.ewm(span=5, adjust=False).mean()
        atr_rsi = (rsi_ma - rsi_ma.shift(1)).abs().ewm(span=14, adjust=False).mean()
        delta = atr_rsi * 4.236
        dataframe["qqe_rsi"] = rsi_ma
        dataframe["qqe_sig"] = rsi_ma.ewm(span=5, adjust=False).mean()
        dataframe["qqe_upper"] = rsi_ma + delta
        dataframe["qqe_lower"] = rsi_ma - delta
        # Trend flip when RSI_MA crosses the opposite trailing edge
        lb = (rsi_ma - delta).to_numpy(copy=True)
        sb = (rsi_ma + delta).to_numpy(copy=True)
        trend = np.zeros(len(rsi_ma), dtype=int)
        for i in range(1, len(rsi_ma)):
            if rsi_ma.iloc[i] > lb[i - 1] and rsi_ma.iloc[i - 1] > lb[i - 1]:
                lb[i] = max(lb[i], lb[i - 1])
            if rsi_ma.iloc[i] < sb[i - 1] and rsi_ma.iloc[i - 1] < sb[i - 1]:
                sb[i] = min(sb[i], sb[i - 1])
            if rsi_ma.iloc[i] > sb[i] and rsi_ma.iloc[i - 1] <= sb[i - 1]:
                trend[i] = 1
            elif rsi_ma.iloc[i] < lb[i] and rsi_ma.iloc[i - 1] >= lb[i - 1]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]
        dataframe["qqe_trend"] = trend
        dataframe["qqe_trend_prev"] = Series(trend).shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        # Primary: QQE trend flip; secondary: RSI_MA / signal cross near mid
        flip_up = (dataframe["qqe_trend_prev"] <= 0) & (dataframe["qqe_trend"] > 0)
        flip_dn = (dataframe["qqe_trend_prev"] >= 0) & (dataframe["qqe_trend"] < 0)
        cross_up = qtpylib.crossed_above(dataframe["qqe_rsi"], dataframe["qqe_sig"])
        cross_dn = qtpylib.crossed_below(dataframe["qqe_rsi"], dataframe["qqe_sig"])
        mid_long = cross_up & (dataframe["qqe_rsi"] < 55) & (dataframe["qqe_rsi"] > 35)
        mid_short = cross_dn & (dataframe["qqe_rsi"] > 45) & (dataframe["qqe_rsi"] < 65)
        dataframe.loc[(flip_up | mid_long) & vol & (dataframe["adx"] > 12), "enter_long"] = 1
        dataframe.loc[(flip_dn | mid_short) & vol & (dataframe["adx"] > 12), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["qqe_trend"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["qqe_trend"] > 0, "exit_short"] = 1
        return dataframe


class DemarkerReclaimStrategy(_LiteBase):
    """DeMarker — fade extremes reclaiming 0.30 / 0.70."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up = (dataframe["high"] - dataframe["high"].shift(1)).clip(lower=0.0)
        dn = (dataframe["low"].shift(1) - dataframe["low"]).clip(lower=0.0)
        dem_up = up.rolling(14).sum()
        dem_dn = dn.rolling(14).sum()
        dataframe["dem"] = dem_up / (dem_up + dem_dn).replace(0, np.nan)
        dataframe["dem_prev"] = dataframe["dem"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        reclaim_up = (dataframe["dem_prev"] < 0.30) & (dataframe["dem"] >= 0.30)
        reclaim_dn = (dataframe["dem_prev"] > 0.70) & (dataframe["dem"] <= 0.70)
        dataframe.loc[reclaim_up & chop & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & chop & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["dem"] > 0.5, "exit_long"] = 1
        dataframe.loc[dataframe["dem"] < 0.5, "exit_short"] = 1
        return dataframe


class KstSignalStrategy(_LiteBase):
    """Know Sure Thing — weighted multi-ROC with signal EMA (Pring)."""

    stoploss = -0.017
    minimal_roi = {"0": 0.012, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 110
    startup_candle_count = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        c = dataframe["close"]
        r1 = c.pct_change(10) * 100
        r2 = c.pct_change(15) * 100
        r3 = c.pct_change(20) * 100
        r4 = c.pct_change(30) * 100
        kst = (
            r1.rolling(10).mean() * 1.0
            + r2.rolling(10).mean() * 2.0
            + r3.rolling(10).mean() * 3.0
            + r4.rolling(15).mean() * 4.0
        )
        dataframe["kst"] = kst
        dataframe["kst_sig"] = kst.rolling(9).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = qtpylib.crossed_above(dataframe["kst"], dataframe["kst_sig"])
        dn = qtpylib.crossed_below(dataframe["kst"], dataframe["kst_sig"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["kst"], dataframe["kst_sig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["kst"], dataframe["kst_sig"]), "exit_short"
        ] = 1
        return dataframe


class RviCrossStrategy(_LiteBase):
    """Relative Vigor Index — RVI / signal cross (open-close vs range)."""

    stoploss = -0.015
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        num = dataframe["close"] - dataframe["open"]
        den = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        n = (
            num
            + 2 * num.shift(1)
            + 2 * num.shift(2)
            + num.shift(3)
        ) / 6.0
        d = (
            den
            + 2 * den.shift(1)
            + 2 * den.shift(2)
            + den.shift(3)
        ) / 6.0
        rvi = n.rolling(10).sum() / d.rolling(10).sum().replace(0, np.nan)
        dataframe["rvi"] = rvi
        dataframe["rvi_sig"] = (
            rvi + 2 * rvi.shift(1) + 2 * rvi.shift(2) + rvi.shift(3)
        ) / 6.0
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        chop = dataframe["adx"] < 30
        up = qtpylib.crossed_above(dataframe["rvi"], dataframe["rvi_sig"])
        dn = qtpylib.crossed_below(dataframe["rvi"], dataframe["rvi_sig"])
        dataframe.loc[up & chop & vol & (dataframe["rvi"] < 0), "enter_long"] = 1
        dataframe.loc[dn & chop & vol & (dataframe["rvi"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["rvi"], dataframe["rvi_sig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["rvi"], dataframe["rvi_sig"]), "exit_short"
        ] = 1
        return dataframe


class VwapReclaimStrategy(_LiteBase):
    """Session-rolling VWAP reclaim — institutional fair-value bounce (2026 TA guides)."""

    stoploss = -0.014
    minimal_roi = {"0": 0.009, "35": 0.0045, "90": 0}
    pair_cooldown_minutes = 80
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Rolling VWAP proxy (crypto 24/7 has no RTH session)
        tp = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0
        vol = dataframe["volume"].replace(0, np.nan)
        win = 48  # ~4h on 5m
        dataframe["vwap"] = (tp * vol).rolling(win).sum() / vol.rolling(win).sum()
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        # reclaim VWAP after dip / rejection
        up = qtpylib.crossed_above(dataframe["close"], dataframe["vwap"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["vwap"])
        long_ok = (dataframe["rsi"] > 40) & (dataframe["rsi"] < 65) & (dataframe["close"] > dataframe["ema50"])
        short_ok = (dataframe["rsi"] < 60) & (dataframe["rsi"] > 35) & (dataframe["close"] < dataframe["ema50"])
        dataframe.loc[up & vol & long_ok & (dataframe["adx"] > 14), "enter_long"] = 1
        dataframe.loc[dn & vol & short_ok & (dataframe["adx"] > 14), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["vwap"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["vwap"], "exit_short"] = 1
        return dataframe


class RsiDivergenceStrategy(_LiteBase):
    """Regular RSI divergence vs swing highs/lows (preferred over raw RSI levels in 2026 guides)."""

    stoploss = -0.016
    minimal_roi = {"0": 0.011, "45": 0.0055, "120": 0}
    pair_cooldown_minutes = 120
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        look = 5
        dataframe["swing_low"] = dataframe["low"] == dataframe["low"].rolling(look * 2 + 1, center=True).min()
        dataframe["swing_high"] = dataframe["high"] == dataframe["high"].rolling(look * 2 + 1, center=True).max()
        # Previous swing values via shift of masked series
        low_at_swing = dataframe["low"].where(dataframe["swing_low"])
        rsi_at_swing_low = dataframe["rsi"].where(dataframe["swing_low"])
        high_at_swing = dataframe["high"].where(dataframe["swing_high"])
        rsi_at_swing_high = dataframe["rsi"].where(dataframe["swing_high"])
        dataframe["prev_swing_low"] = low_at_swing.ffill().shift(1)
        dataframe["prev_rsi_swing_low"] = rsi_at_swing_low.ffill().shift(1)
        dataframe["prev_swing_high"] = high_at_swing.ffill().shift(1)
        dataframe["prev_rsi_swing_high"] = rsi_at_swing_high.ffill().shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        # Bullish: lower low in price, higher RSI; Bearish: higher high, lower RSI
        bull = (
            dataframe["swing_low"].fillna(False)
            & (dataframe["low"] < dataframe["prev_swing_low"])
            & (dataframe["rsi"] > dataframe["prev_rsi_swing_low"])
            & (dataframe["rsi"] < 40)
        )
        bear = (
            dataframe["swing_high"].fillna(False)
            & (dataframe["high"] > dataframe["prev_swing_high"])
            & (dataframe["rsi"] < dataframe["prev_rsi_swing_high"])
            & (dataframe["rsi"] > 60)
        )
        # center=True uses future bars — shift signal by look to avoid lookahead
        bull = bull.shift(5).fillna(False)
        bear = bear.shift(5).fillna(False)
        dataframe.loc[bull & vol & (dataframe["adx"] < 32), "enter_long"] = 1
        dataframe.loc[bear & vol & (dataframe["adx"] < 32), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 55, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 45, "exit_short"] = 1
        return dataframe


class ChandelierFlipStrategy(_LiteBase):
    """Chandelier Exit flip — ATR trail from highest high / lowest low (trend following)."""

    stoploss = -0.02
    minimal_roi = {"0": 0.014, "60": 0.007, "180": 0}
    pair_cooldown_minutes = 120
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr = ta.ATR(dataframe, timeperiod=22)
        hh = dataframe["high"].rolling(22).max()
        ll = dataframe["low"].rolling(22).min()
        dataframe["ch_long"] = hh - 3.0 * atr
        dataframe["ch_short"] = ll + 3.0 * atr
        # Direction: close vs mid of trails
        mid = (dataframe["ch_long"] + dataframe["ch_short"]) / 2.0
        dataframe["ch_dir"] = np.where(dataframe["close"] > mid, 1, -1)
        dataframe["ch_dir_prev"] = Series(dataframe["ch_dir"]).shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 18
        flip_up = (dataframe["ch_dir_prev"] <= 0) & (dataframe["ch_dir"] > 0)
        flip_dn = (dataframe["ch_dir_prev"] >= 0) & (dataframe["ch_dir"] < 0)
        dataframe.loc[flip_up & vol & trend, "enter_long"] = 1
        dataframe.loc[flip_dn & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ch_long"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ch_short"], "exit_short"] = 1
        return dataframe


class ZscoreMeanRevStrategy(_LiteBase):
    """Z-score mean reversion — fade |z|>2 when ADX low / ATR not expanding (Vantixs/BloFin)."""

    stoploss = -0.014
    minimal_roi = {"0": 0.009, "35": 0.0045, "90": 0}
    pair_cooldown_minutes = 80
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        win = 20
        ma = dataframe["close"].rolling(win).mean()
        sd = dataframe["close"].rolling(win).std().replace(0, np.nan)
        dataframe["zscore"] = (dataframe["close"] - ma) / sd
        dataframe["z_prev"] = dataframe["zscore"].shift(1)
        dataframe["bb_mid"] = ma
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_ma"] = dataframe["atr"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ranging = dataframe["adx"] < 25
        atr_ok = dataframe["atr"] < dataframe["atr_ma"] * 1.5
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        # enter on extreme, or reclaim from extreme
        long_sig = (dataframe["zscore"] <= -2.0) | (
            (dataframe["z_prev"] < -2.0) & (dataframe["zscore"] >= -2.0)
        )
        short_sig = (dataframe["zscore"] >= 2.0) | (
            (dataframe["z_prev"] > 2.0) & (dataframe["zscore"] <= 2.0)
        )
        dataframe.loc[long_sig & ranging & atr_ok & vol, "enter_long"] = 1
        dataframe.loc[short_sig & ranging & atr_ok & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["bb_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["bb_mid"], "exit_short"] = 1
        return dataframe


class TtmSqueezeStrategy(_LiteBase):
    """TTM Squeeze — BB inside Keltner then momentum release (LazyBear-style)."""

    stoploss = -0.018
    minimal_roi = {"0": 0.013, "50": 0.006, "150": 0}
    pair_cooldown_minutes = 100
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bb = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        mid = ta.EMA(dataframe, timeperiod=20)
        atr = ta.ATR(dataframe, timeperiod=10)
        kc_upper = mid + 1.5 * atr
        kc_lower = mid - 1.5 * atr
        dataframe["bb_upper"] = bb["upper"]
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]
        dataframe["squeeze_on"] = (bb["lower"] > kc_lower) & (bb["upper"] < kc_upper)
        dataframe["squeeze_prev"] = dataframe["squeeze_on"].shift(1).fillna(False)
        # Momentum: close vs Donchian mid, linreg-smoothed proxy
        don_mid = (dataframe["high"].rolling(20).max() + dataframe["low"].rolling(20).min()) / 2.0
        delta = dataframe["close"] - don_mid
        dataframe["sqz_mom"] = delta.rolling(5).mean()
        dataframe["sqz_mom_prev"] = dataframe["sqz_mom"].shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        # squeeze just released
        released = dataframe["squeeze_prev"] & (~dataframe["squeeze_on"])
        mom_up = (dataframe["sqz_mom"] > 0) & (dataframe["sqz_mom"] > dataframe["sqz_mom_prev"])
        mom_dn = (dataframe["sqz_mom"] < 0) & (dataframe["sqz_mom"] < dataframe["sqz_mom_prev"])
        trend = dataframe["adx"] > 14
        dataframe.loc[released & mom_up & vol & trend, "enter_long"] = 1
        dataframe.loc[released & mom_dn & vol & trend, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["sqz_mom"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["sqz_mom"] > 0, "exit_short"] = 1
        return dataframe
