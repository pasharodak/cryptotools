# Day-trading wave — classic intraday playbook adapted to crypto 5m (UTC sessions).
"""ORB breakout, VWAP pullback, EMA9/21, prev-day H/L, NY session momentum,
inside-bar, NR7, momentum burst.
"""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


def _ensure_utc_cols(df: DataFrame) -> DataFrame:
    out = df.copy()
    if "date" not in out.columns:
        return out
    dt = out["date"]
    try:
        if getattr(dt.dt, "tz", None) is None:
            dt = dt.dt.tz_localize("UTC")
        else:
            dt = dt.dt.tz_convert("UTC")
    except (TypeError, AttributeError, ValueError):
        pass
    out["_dt"] = dt
    out["m_from_mid"] = dt.dt.hour * 60 + dt.dt.minute
    out["utc_day"] = dt.dt.floor("D")
    return out


def _session_vwap(df: DataFrame) -> DataFrame:
    """UTC-day session VWAP (resets each midnight)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    cum_pv = pv.groupby(df["utc_day"], sort=False).cumsum()
    cum_v = df["volume"].groupby(df["utc_day"], sort=False).cumsum().replace(0, np.nan)
    df["vwap"] = (cum_pv / cum_v).astype(float)
    return df


class DayOrbBreakoutStrategy(_LiteBase):
    """Opening Range Breakout — break UTC first 15m high/low with volume (continuation)."""

    stoploss = -0.009
    minimal_roi = {"0": 0.012, "45": 0.006, "120": 0}
    pair_cooldown_minutes = 180
    startup_candle_count = 80
    or_end_minute = 15
    entry_until_minute = 240  # first 4h after midnight UTC

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = _ensure_utc_cols(dataframe)
        if "utc_day" not in df.columns:
            return dataframe
        in_or = df["m_from_mid"] < self.or_end_minute
        or_stats = (
            df.loc[in_or]
            .groupby("utc_day", sort=False)
            .agg(or_high=("high", "max"), or_low=("low", "min"))
        )
        df = df.join(or_stats, on="utc_day")
        df = _session_vwap(df)
        df["vol_sma"] = df["volume"].rolling(20).mean()
        df["atr"] = ta.ATR(df, timeperiod=14)
        df["or_width"] = (df["or_high"] - df["or_low"]) / df["close"]
        dataframe["or_high"] = df["or_high"]
        dataframe["or_low"] = df["or_low"]
        dataframe["vwap"] = df["vwap"]
        dataframe["vol_sma"] = df["vol_sma"]
        dataframe["atr"] = df["atr"]
        dataframe["or_width"] = df["or_width"]
        dataframe["m_from_mid"] = df["m_from_mid"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        window = (dataframe["m_from_mid"] >= self.or_end_minute) & (
            dataframe["m_from_mid"] < self.entry_until_minute
        )
        wide_enough = dataframe["or_width"] > 0.002
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.15
        up = qtpylib.crossed_above(dataframe["close"], dataframe["or_high"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["or_low"])
        dataframe.loc[
            window & wide_enough & vol & up & (dataframe["close"] > dataframe["vwap"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            window & wide_enough & vol & dn & (dataframe["close"] < dataframe["vwap"]),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["or_high"] + dataframe["or_low"]) / 2.0
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class DayVwapPullbackStrategy(_LiteBase):
    """VWAP pullback continuation — trend day: touch VWAP then resume with EMA bias."""

    stoploss = -0.008
    minimal_roi = {"0": 0.01, "40": 0.005, "100": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = _ensure_utc_cols(dataframe)
        if "utc_day" not in df.columns:
            return dataframe
        df = _session_vwap(df)
        dataframe["vwap"] = df["vwap"]
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["dist_vwap"] = (dataframe["close"] - dataframe["vwap"]).abs() / dataframe["close"]
        dataframe["touch_vwap"] = dataframe["dist_vwap"] < 0.0015
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        uptrend = (dataframe["ema9"] > dataframe["ema21"]) & (dataframe["close"] > dataframe["vwap"])
        dntrend = (dataframe["ema9"] < dataframe["ema21"]) & (dataframe["close"] < dataframe["vwap"])
        # pullback touch then bullish/bearish close away from VWAP
        long_sig = (
            uptrend
            & dataframe["touch_vwap"].shift(1).fillna(False)
            & (dataframe["close"] > dataframe["vwap"])
            & (dataframe["rsi"] > 45)
            & (dataframe["rsi"] < 70)
        )
        short_sig = (
            dntrend
            & dataframe["touch_vwap"].shift(1).fillna(False)
            & (dataframe["close"] < dataframe["vwap"])
            & (dataframe["rsi"] < 55)
            & (dataframe["rsi"] > 30)
        )
        dataframe.loc[long_sig & vol, "enter_long"] = 1
        dataframe.loc[short_sig & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["vwap"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["vwap"], "exit_short"] = 1
        return dataframe


class DayEmaRibbonStrategy(_LiteBase):
    """Classic day-trade EMA 9/21 ribbon — pullback to fast EMA in trend."""

    stoploss = -0.006
    minimal_roi = {"0": 0.009, "30": 0.0045, "80": 0}
    pair_cooldown_minutes = 60
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.0
        bull = (dataframe["ema9"] > dataframe["ema21"]) & (dataframe["ema21"] > dataframe["ema50"])
        bear = (dataframe["ema9"] < dataframe["ema21"]) & (dataframe["ema21"] < dataframe["ema50"])
        # pullback: low tags ema9 then close back above
        long_pb = (
            bull
            & (dataframe["low"] <= dataframe["ema9"] * 1.001)
            & (dataframe["close"] > dataframe["ema9"])
            & (dataframe["rsi"] > 40)
        )
        short_pb = (
            bear
            & (dataframe["high"] >= dataframe["ema9"] * 0.999)
            & (dataframe["close"] < dataframe["ema9"])
            & (dataframe["rsi"] < 60)
        )
        dataframe.loc[long_pb & vol, "enter_long"] = 1
        dataframe.loc[short_pb & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema21"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema21"], "exit_short"] = 1
        return dataframe


class DayPrevDayBreakStrategy(_LiteBase):
    """Previous-day high/low breakout — classic level day-trade."""

    stoploss = -0.01
    minimal_roi = {"0": 0.012, "50": 0.006, "140": 0}
    pair_cooldown_minutes = 120
    startup_candle_count = 320

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = _ensure_utc_cols(dataframe)
        if "utc_day" not in df.columns:
            return dataframe
        daily = (
            df.set_index("_dt")
            .resample("1D")
            .agg({"high": "max", "low": "min", "close": "last"})
            .dropna(how="any")
        )
        daily["pdh"] = daily["high"].shift(1)
        daily["pdl"] = daily["low"].shift(1)
        # Index as UTC midnight timestamps for map
        idx = daily.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("UTC")
        daily.index = idx.floor("D")
        df["pdh"] = df["utc_day"].map(daily["pdh"])
        df["pdl"] = df["utc_day"].map(daily["pdl"])
        dataframe["pdh"] = df["pdh"].astype(float)
        dataframe["pdl"] = df["pdl"].astype(float)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 16
        up = qtpylib.crossed_above(dataframe["close"], dataframe["pdh"])
        dn = qtpylib.crossed_below(dataframe["close"], dataframe["pdl"])
        dataframe.loc[up & vol & trend & (dataframe["close"] > dataframe["ema21"]), "enter_long"] = 1
        dataframe.loc[dn & vol & trend & (dataframe["close"] < dataframe["ema21"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["pdh"] + dataframe["pdl"]) / 2.0
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class DayNyMomentumStrategy(_LiteBase):
    """US cash overlap momentum — trade only 13:30–20:00 UTC with EMA impulse."""

    stoploss = -0.006
    minimal_roi = {"0": 0.009, "25": 0.0045, "70": 0}
    pair_cooldown_minutes = 45
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = _ensure_utc_cols(dataframe)
        dataframe["m_from_mid"] = df.get("m_from_mid", 0)
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=3)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # NYSE cash ~13:30–20:00 UTC
        ny = (dataframe["m_from_mid"] >= 13 * 60 + 30) & (dataframe["m_from_mid"] < 20 * 60)
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.2
        up = qtpylib.crossed_above(dataframe["ema9"], dataframe["ema21"]) & (dataframe["roc"] > 0.15)
        dn = qtpylib.crossed_below(dataframe["ema9"], dataframe["ema21"]) & (dataframe["roc"] < -0.15)
        dataframe.loc[ny & vol & up & (dataframe["rsi"] < 75), "enter_long"] = 1
        dataframe.loc[ny & vol & dn & (dataframe["rsi"] > 25), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["ema9"], dataframe["ema21"]), "exit_long"
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["ema9"], dataframe["ema21"]), "exit_short"
        ] = 1
        return dataframe


class DayInsideBarBreakStrategy(_LiteBase):
    """Inside-bar breakout — mother bar range break with volume."""

    stoploss = -0.007
    minimal_roi = {"0": 0.01, "35": 0.005, "90": 0}
    pair_cooldown_minutes = 75
    startup_candle_count = 40

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hi, lo = dataframe["high"], dataframe["low"]
        # Current bar is inside the previous bar's range
        inside = (hi < hi.shift(1)) & (lo > lo.shift(1))
        # On breakout bar t: t-1 was inside, mother = t-2
        dataframe["was_inside"] = inside.shift(1).fillna(False)
        dataframe["mother_hi"] = hi.shift(2)
        dataframe["mother_lo"] = lo.shift(2)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        trend = dataframe["adx"] > 14
        up = dataframe["was_inside"] & (dataframe["close"] > dataframe["mother_hi"])
        dn = dataframe["was_inside"] & (dataframe["close"] < dataframe["mother_lo"])
        dataframe.loc[up & vol & trend & (dataframe["close"] > dataframe["ema21"]), "enter_long"] = 1
        dataframe.loc[dn & vol & trend & (dataframe["close"] < dataframe["ema21"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["mother_hi"] + dataframe["mother_lo"]) / 2.0
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class DayNr7BreakStrategy(_LiteBase):
    """NR7 (Toby Crabel) — narrowest range of 7 bars → breakout."""

    stoploss = -0.008
    minimal_roi = {"0": 0.011, "40": 0.0055, "100": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 40

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rng = dataframe["high"] - dataframe["low"]
        dataframe["range"] = rng
        dataframe["nr7"] = rng == rng.rolling(7).min()
        dataframe["nr7_hi"] = dataframe["high"].where(dataframe["nr7"]).ffill()
        dataframe["nr7_lo"] = dataframe["low"].where(dataframe["nr7"]).ffill()
        dataframe["nr7_prev"] = dataframe["nr7"].shift(1).fillna(False)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        up = dataframe["nr7_prev"] & (dataframe["close"] > dataframe["nr7_hi"])
        dn = dataframe["nr7_prev"] & (dataframe["close"] < dataframe["nr7_lo"])
        dataframe.loc[up & vol & (dataframe["close"] > dataframe["ema21"]), "enter_long"] = 1
        dataframe.loc[dn & vol & (dataframe["close"] < dataframe["ema21"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema21"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema21"], "exit_short"] = 1
        return dataframe


class DayMomentumBurstStrategy(_LiteBase):
    """Momentum burst — 3 consecutive directional bars + volume surge (gap-and-go proxy)."""

    stoploss = -0.006
    minimal_roi = {"0": 0.01, "20": 0.005, "60": 0}
    pair_cooldown_minutes = 60
    startup_candle_count = 40

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        c, o = dataframe["close"], dataframe["open"]
        up_bar = c > o
        dn_bar = c < o
        dataframe["three_up"] = up_bar & up_bar.shift(1) & up_bar.shift(2)
        dataframe["three_dn"] = dn_bar & dn_bar.shift(1) & dn_bar.shift(2)
        dataframe["body"] = (c - o).abs() / c
        dataframe["body_sma"] = dataframe["body"].rolling(20).mean()
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.4
        strong = dataframe["body"] > dataframe["body_sma"] * 1.2
        dataframe.loc[
            dataframe["three_up"] & vol & strong & (dataframe["rsi"] < 80) & (dataframe["close"] > dataframe["ema21"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            dataframe["three_dn"] & vol & strong & (dataframe["rsi"] > 20) & (dataframe["close"] < dataframe["ema21"]),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 75, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 25, "exit_short"] = 1
        return dataframe
