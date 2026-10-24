# Wave 5: indicators not yet covered in chart1–4 / combo waves.
"""CHOP, NATR, Connors RSI, DEMA, APO, BOP, LinReg slope, Mass Index, Force Index, CVD proxy, RS vs BTC."""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from ctengine.strategy.strategy_helper import merge_informative_pair
from pandas import DataFrame, Series
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


def _choppiness_index(df: DataFrame, period: int = 14) -> Series:
    """CHOP: high = choppy range, low = trending (TradingView formula)."""
    tr = ta.TRANGE(df)
    atr_sum = tr.rolling(period).sum()
    hi = df["high"].rolling(period).max()
    lo = df["low"].rolling(period).min()
    span = (hi - lo).replace(0, np.nan)
    return 100.0 * np.log10(atr_sum / span) / np.log10(period)


def _up_down_streak(close: Series) -> Series:
    """Vectorized consecutive up(+)/down(−) close streak."""
    diff = close.diff()
    sign = np.sign(diff.to_numpy(dtype=float))
    sign[np.isnan(sign)] = 0.0
    out = np.zeros(len(sign), dtype=float)
    for i in range(1, len(sign)):
        s = sign[i]
        if s == 0:
            out[i] = 0.0
        elif s == sign[i - 1] or (sign[i - 1] == 0 and out[i - 1] == 0):
            # continue or start streak in direction s
            prev = out[i - 1]
            if prev == 0 or np.sign(prev) == s:
                out[i] = prev + s
            else:
                out[i] = s
        else:
            out[i] = s
    return Series(out, index=close.index)


def _connors_rsi(close: Series) -> Series:
    """Connors RSI = avg(RSI(3), StreakRSI(2), PercentRank(100 of 1-bar ROC))."""
    rsi3 = ta.RSI(close, timeperiod=3)
    streak = _up_down_streak(close)
    streak_rsi = ta.RSI(streak, timeperiod=2)
    roc1 = close.pct_change()
    # Approx percent-rank via rolling rank (faster than python apply)
    pct_rank = roc1.rolling(100).rank(pct=True) * 100.0
    return (rsi3 + streak_rsi + pct_rank) / 3.0


def _mass_index(df: DataFrame, ema_period: int = 9, sum_period: int = 25) -> Series:
    rng = (df["high"] - df["low"]).astype(float)
    ema1 = rng.ewm(span=ema_period, adjust=False).mean()
    ema2 = ema1.ewm(span=ema_period, adjust=False).mean()
    ratio = ema1 / ema2.replace(0, np.nan)
    return ratio.rolling(sum_period).sum()


def _force_index(df: DataFrame, period: int = 13) -> Series:
    raw = df["close"].diff() * df["volume"]
    return raw.ewm(span=period, adjust=False).mean()


def _cvd_proxy(df: DataFrame) -> Series:
    """OHLCV approximation of cumulative volume delta (no tick tape)."""
    hi = df["high"].astype(float)
    lo = df["low"].astype(float)
    cl = df["close"].astype(float)
    vol = df["volume"].astype(float)
    span = (hi - lo).replace(0, np.nan)
    buy_share = ((cl - lo) / span).clip(0, 1).fillna(0.5)
    delta = vol * (2 * buy_share - 1)
    return delta.cumsum()


class ChopFadeStrategy(_LiteBase):
    """Choppiness Index — fade extremes when CHOP high (range), skip when trending."""

    stoploss = -0.009
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["chop"] = _choppiness_index(dataframe, 14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["natr"] = ta.NATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        choppy = dataframe["chop"] > 55.0
        fee_ok = dataframe["natr"] >= 0.12  # NATR already in %
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        dataframe.loc[
            choppy & fee_ok & vol & (dataframe["rsi"] < 35),
            "enter_long",
        ] = 1
        dataframe.loc[
            choppy & fee_ok & vol & (dataframe["rsi"] > 65),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] > 55) | (dataframe["chop"] < 40), "exit_long"] = 1
        dataframe.loc[(dataframe["rsi"] < 45) | (dataframe["chop"] < 40), "exit_short"] = 1
        return dataframe


class NatrBreakoutStrategy(_LiteBase):
    """NATR regime — break Donchian only when normalized ATR is elevated (move > fees)."""

    stoploss = -0.006
    minimal_roi = {"0": 0.012, "45": 0.006, "130": 0}
    pair_cooldown_minutes = 100
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["natr"] = ta.NATR(dataframe, timeperiod=14)
        dataframe["natr_ma"] = dataframe["natr"].rolling(50).mean()
        dataframe["don_hi"] = dataframe["high"].rolling(20).max()
        dataframe["don_lo"] = dataframe["low"].rolling(20).min()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hot = (dataframe["natr"] > dataframe["natr_ma"] * 1.15) & (dataframe["natr"] >= 0.18)
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 18
        up = qtpylib.crossed_above(dataframe["close"], dataframe["don_hi"].shift(1))
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["don_lo"].shift(1))
        dataframe.loc[hot & vol & trend & up & (dataframe["close"] > dataframe["ema50"]), "enter_long"] = 1
        dataframe.loc[hot & vol & trend & dn & (dataframe["close"] < dataframe["ema50"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["don_hi"] + dataframe["don_lo"]) / 2.0
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class ConnorsRsiStrategy(_LiteBase):
    """Connors RSI — short-term mean reversion on CRSI extremes."""

    stoploss = -0.008
    minimal_roi = {"0": 0.009, "30": 0.0045, "90": 0}
    pair_cooldown_minutes = 75
    startup_candle_count = 120

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["crsi"] = _connors_rsi(dataframe["close"].astype(float))
        dataframe["crsi_prev"] = dataframe["crsi"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ranging = dataframe["adx"] < 28
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        reclaim_up = (dataframe["crsi_prev"] < 15) & (dataframe["crsi"] >= 15)
        reclaim_dn = (dataframe["crsi_prev"] > 85) & (dataframe["crsi"] <= 85)
        dataframe.loc[reclaim_up & ranging & vol, "enter_long"] = 1
        dataframe.loc[reclaim_dn & ranging & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["crsi"] > 55, "exit_long"] = 1
        dataframe.loc[dataframe["crsi"] < 45, "exit_short"] = 1
        return dataframe


class DemaCrossStrategy(_LiteBase):
    """DEMA 10/30 cross — lower-lag trend following vs classic EMA."""

    stoploss = -0.01
    minimal_roi = {"0": 0.011, "45": 0.0055, "120": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["dema_fast"] = ta.DEMA(dataframe, timeperiod=10)
        dataframe["dema_slow"] = ta.DEMA(dataframe, timeperiod=30)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["natr"] = ta.NATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"].between(16, 40)
        fee_ok = dataframe["natr"] >= 0.12
        up = qtpylib.crossed_above(dataframe["dema_fast"], dataframe["dema_slow"])
        dn = qtpylib.crossed_below(dataframe["dema_fast"], dataframe["dema_slow"])
        dataframe.loc[up & vol & trend & fee_ok, "enter_long"] = 1
        dataframe.loc[dn & vol & trend & fee_ok, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["dema_fast"], dataframe["dema_slow"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["dema_fast"], dataframe["dema_slow"]), "exit_short"
        ] = 1
        return dataframe


class ApoMomentumStrategy(_LiteBase):
    """APO — absolute price oscillator zero-line / signal flips."""

    stoploss = -0.009
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["apo"] = ta.APO(dataframe, fastperiod=12, slowperiod=26, matype=1)
        dataframe["apo_sig"] = ta.EMA(dataframe["apo"], timeperiod=9)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = qtpylib.crossed_above(dataframe["apo"], dataframe["apo_sig"])
        dn = qtpylib.crossed_below(dataframe["apo"], dataframe["apo_sig"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["apo"], dataframe["apo_sig"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["apo"], dataframe["apo_sig"]), "exit_short"
        ] = 1
        return dataframe


class BopReversalStrategy(_LiteBase):
    """Balance of Power — fade BOP extremes with EMA bias."""

    stoploss = -0.008
    minimal_roi = {"0": 0.009, "35": 0.0045, "95": 0}
    pair_cooldown_minutes = 80
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["bop"] = ta.BOP(dataframe)
        dataframe["bop_sma"] = dataframe["bop"].rolling(14).mean()
        dataframe["bop_prev"] = dataframe["bop_sma"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        mild = dataframe["adx"] < 35
        # Reclaim from soft extremes (BOP SMA is usually small on 5m)
        up = (dataframe["bop_prev"] < -0.05) & (dataframe["bop_sma"] > dataframe["bop_prev"]) & (
            dataframe["bop_sma"] >= -0.02
        )
        dn = (dataframe["bop_prev"] > 0.05) & (dataframe["bop_sma"] < dataframe["bop_prev"]) & (
            dataframe["bop_sma"] <= 0.02
        )
        dataframe.loc[up & vol & mild, "enter_long"] = 1
        dataframe.loc[dn & vol & mild, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["bop_sma"] > 0.05, "exit_long"] = 1
        dataframe.loc[dataframe["bop_sma"] < -0.05, "exit_short"] = 1
        return dataframe


class LinregSlopeStrategy(_LiteBase):
    """Linear regression slope — enter with slope flip + ADX filter."""

    stoploss = -0.01
    minimal_roi = {"0": 0.011, "45": 0.0055, "120": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["lr_slope"] = ta.LINEARREG_SLOPE(dataframe, timeperiod=20)
        dataframe["lr_slope_prev"] = dataframe["lr_slope"].shift(1)
        dataframe["lr"] = ta.LINEARREG(dataframe, timeperiod=20)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        up = (dataframe["lr_slope_prev"] <= 0) & (dataframe["lr_slope"] > 0)
        dn = (dataframe["lr_slope_prev"] >= 0) & (dataframe["lr_slope"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["lr_slope"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["lr_slope"] > 0, "exit_short"] = 1
        return dataframe


class MassIndexStrategy(_LiteBase):
    """Mass Index reversal — bulge >27 then drop below 26.5 (Bulkowski)."""

    stoploss = -0.011
    minimal_roi = {"0": 0.012, "50": 0.006, "140": 0}
    pair_cooldown_minutes = 110
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["mass"] = _mass_index(dataframe)
        dataframe["mass_prev"] = dataframe["mass"].shift(1)
        dataframe["mass_max20"] = dataframe["mass"].rolling(20).max()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        # Recent bulge then cross back under 26.5
        setup = dataframe["mass_max20"] > 27.0
        trigger = (dataframe["mass_prev"] >= 26.5) & (dataframe["mass"] < 26.5)
        dataframe.loc[
            setup & trigger & vol & (dataframe["close"] > dataframe["ema50"]) & (dataframe["rsi"] < 60),
            "enter_long",
        ] = 1
        dataframe.loc[
            setup & trigger & vol & (dataframe["close"] < dataframe["ema50"]) & (dataframe["rsi"] > 40),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 70, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 30, "exit_short"] = 1
        return dataframe


class ForceIndexStrategy(_LiteBase):
    """Elder Force Index — zero-line cross with EMA trend filter."""

    stoploss = -0.009
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["fi"] = _force_index(dataframe, 13)
        dataframe["fi_prev"] = dataframe["fi"].shift(1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        trend = dataframe["adx"] > 15
        up = (dataframe["fi_prev"] <= 0) & (dataframe["fi"] > 0)
        dn = (dataframe["fi_prev"] >= 0) & (dataframe["fi"] < 0)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["fi"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["fi"] > 0, "exit_short"] = 1
        return dataframe


class CvdProxyStrategy(_LiteBase):
    """CVD proxy from OHLCV — trade delta EMA crosses in liquid sessions."""

    stoploss = -0.009
    minimal_roi = {"0": 0.01, "40": 0.005, "110": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cvd = _cvd_proxy(dataframe)
        dataframe["cvd"] = cvd
        dataframe["cvd_ema"] = cvd.ewm(span=21, adjust=False).mean()
        dataframe["cvd_ema_slow"] = cvd.ewm(span=55, adjust=False).mean()
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 14
        up = qtpylib.crossed_above(dataframe["cvd_ema"], dataframe["cvd_ema_slow"])
        dn = qtpylib.crossed_below(dataframe["cvd_ema"], dataframe["cvd_ema_slow"])
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["cvd_ema"], dataframe["cvd_ema_slow"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["cvd_ema"], dataframe["cvd_ema_slow"]), "exit_short"
        ] = 1
        return dataframe


class RsBtcMomentumStrategy(_LiteBase):
    """Relative strength vs BTC — trade when pair ROC outperforms/underperforms BTC."""

    stoploss = -0.012
    minimal_roi = {"0": 0.013, "50": 0.0065, "140": 0}
    pair_cooldown_minutes = 120
    startup_candle_count = 80

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["roc20"] = ta.ROC(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        pair = (metadata or {}).get("pair") or ""
        if pair != "BTC/USDT:USDT" and self.dp is not None:
            try:
                btc = self.dp.get_pair_dataframe(pair="BTC/USDT:USDT", timeframe=self.timeframe)
                if btc is not None and not btc.empty:
                    btc = btc[["date", "close"]].copy()
                    btc["btc_roc20"] = ta.ROC(btc["close"], timeperiod=20)
                    dataframe = merge_informative_pair(
                        dataframe, btc, self.timeframe, self.timeframe, ffill=True
                    )
            except Exception:
                pass
        if "btc_roc20_5m" in dataframe.columns:
            dataframe["rs_btc"] = dataframe["roc20"] - dataframe["btc_roc20_5m"]
        elif "btc_roc20" in dataframe.columns:
            dataframe["rs_btc"] = dataframe["roc20"] - dataframe["btc_roc20"]
        else:
            dataframe["rs_btc"] = dataframe["roc20"]
        dataframe["rs_prev"] = dataframe["rs_btc"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        trend = dataframe["adx"] > 16
        # RS crosses above/below zero = relative strength flip
        up = (dataframe["rs_prev"] <= 0) & (dataframe["rs_btc"] > 0.3)
        dn = (dataframe["rs_prev"] >= 0) & (dataframe["rs_btc"] < -0.3)
        dataframe.loc[
            up & vol & trend & (dataframe["close"] > dataframe["ema50"]), "enter_long"
        ] = 1
        dataframe.loc[
            dn & vol & trend & (dataframe["close"] < dataframe["ema50"]), "enter_short"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rs_btc"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["rs_btc"] > 0, "exit_short"] = 1
        return dataframe
