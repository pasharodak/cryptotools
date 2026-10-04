# Pattern Scalp (Doug / YouTube) — crypto adaptation for 5m Bybit perps.
"""Opening-range manipulation fade: first 15m of UTC day vs daily ATR, then JW/engulf entry.

Video rules (stocks RTH open) mapped to crypto:
1) Box high/low of first 15 minutes after UTC midnight (3×5m bars).
2) Manipulation if OR range ≥ 20% of daily ATR(14); fade the OR direction.
3) On 5m wait for John Wick (hammer / inv-hammer) or Power Tower (engulfing),
   enter on break of that confirmation candle; target opposite OR extreme.
"""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class PatternScalpStrategy(_LiteBase):
    """Pattern Scalp — UTC open manipulation reverse."""

    stoploss = -0.02
    # Soft ROI backup; primary exit is OR opposite side via exit signals.
    minimal_roi = {"0": 0.025, "60": 0.015, "180": 0.008, "360": 0}
    pair_cooldown_minutes = 180
    startup_candle_count = 320

    # Session: minutes from UTC midnight. OR = [0,15), entries until +75m.
    or_end_minute = 15
    entry_until_minute = 90
    manip_atr_frac = 0.20
    # Prefer stronger flushes when True (video prefers 70–80% ATR; keep 20% default).
    prefer_strong_manip = False
    strong_manip_atr_frac = 0.50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        if "date" not in df.columns:
            return dataframe

        dt = df["date"]
        # Ensure timezone-aware UTC for session math.
        try:
            if getattr(dt.dt, "tz", None) is None:
                dt = dt.dt.tz_localize("UTC")
            else:
                dt = dt.dt.tz_convert("UTC")
        except (TypeError, AttributeError, ValueError):
            pass

        df["_dt"] = dt
        df["m_from_mid"] = dt.dt.hour * 60 + dt.dt.minute
        df["utc_day"] = dt.dt.floor("D")

        # Daily ATR(14) from 1D resample, forward-filled onto 5m.
        ohlc = (
            df.set_index("_dt")
            .resample("1D")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna(how="any")
        )
        if len(ohlc) >= 20:
            daily_atr = ta.ATR(ohlc, timeperiod=14)
            atr_map = daily_atr.shift(1)  # prior day ATR only (no lookahead)
            df["daily_atr"] = df["_dt"].dt.floor("D").map(atr_map).astype(float)
        else:
            df["daily_atr"] = np.nan

        # Opening-range stats per UTC day from first three 5m bars (0,5,10).
        in_or = df["m_from_mid"] < self.or_end_minute
        or_stats = (
            df.loc[in_or]
            .groupby("utc_day", sort=False)
            .agg(
                or_high=("high", "max"),
                or_low=("low", "min"),
                or_open=("open", "first"),
                or_close=("close", "last"),
            )
        )
        or_stats["or_range"] = or_stats["or_high"] - or_stats["or_low"]
        or_stats["or_mid"] = (or_stats["or_high"] + or_stats["or_low"]) / 2.0
        # Bearish manipulation (dump) → fade long; bullish (pump) → fade short.
        or_stats["manip_down"] = or_stats["or_close"] < or_stats["or_mid"]
        or_stats["manip_up"] = or_stats["or_close"] > or_stats["or_mid"]

        for col in ("or_high", "or_low", "or_range", "or_mid", "manip_down", "manip_up"):
            df[col] = df["utc_day"].map(or_stats[col])

        atr = df["daily_atr"].replace(0, np.nan)
        frac = df["or_range"] / atr
        min_frac = (
            self.strong_manip_atr_frac if self.prefer_strong_manip else self.manip_atr_frac
        )
        df["is_manip"] = (frac >= min_frac).fillna(False)
        df["manip_frac"] = frac

        body = (df["close"] - df["open"]).abs()
        lower_w = np.minimum(df["open"], df["close"]) - df["low"]
        upper_w = df["high"] - np.maximum(df["open"], df["close"])
        body_safe = body.replace(0, np.nan)

        # John Wick: hammer (long) / inverted hammer-shooting (short).
        df["jw_long"] = (lower_w >= 1.5 * body_safe) & (upper_w <= body_safe) & (body > 0)
        df["jw_short"] = (upper_w >= 1.5 * body_safe) & (lower_w <= body_safe) & (body > 0)

        # Power Tower: engulfing.
        prev_o = df["open"].shift(1)
        prev_c = df["close"].shift(1)
        df["pt_long"] = (
            (df["close"] > df["open"])
            & (prev_c < prev_o)
            & (df["close"] >= prev_o)
            & (df["open"] <= prev_c)
        )
        df["pt_short"] = (
            (df["close"] < df["open"])
            & (prev_c > prev_o)
            & (df["close"] <= prev_o)
            & (df["open"] >= prev_c)
        )

        df["conf_long"] = (df["jw_long"] | df["pt_long"]).fillna(False)
        df["conf_short"] = (df["jw_short"] | df["pt_short"]).fillna(False)

        # OR complete after minute >= 15.
        df["or_ready"] = df["m_from_mid"] >= self.or_end_minute
        df["in_entry_window"] = (df["m_from_mid"] >= self.or_end_minute) & (
            df["m_from_mid"] < self.entry_until_minute
        )

        # Break of prior confirmation candle.
        df["break_long"] = df["conf_long"].shift(1).fillna(False) & (
            df["close"] > df["high"].shift(1)
        )
        df["break_short"] = df["conf_short"].shift(1).fillna(False) & (
            df["close"] < df["low"].shift(1)
        )

        drop_cols = [c for c in ("_dt",) if c in df.columns]
        return df.drop(columns=drop_cols, errors="ignore")

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        window = dataframe["in_entry_window"].fillna(False)
        manip = dataframe["is_manip"].fillna(False)
        ready = dataframe["or_ready"].fillna(False)

        long_setup = (
            window
            & ready
            & manip
            & dataframe["manip_down"].fillna(False)
            & dataframe["break_long"].fillna(False)
        )
        short_setup = (
            window
            & ready
            & manip
            & dataframe["manip_up"].fillna(False)
            & dataframe["break_short"].fillna(False)
        )
        dataframe.loc[long_setup, "enter_long"] = 1
        dataframe.loc[short_setup, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Target = opposite side of opening range (video: back to OR extreme).
        hit_or_high = dataframe["close"] >= dataframe["or_high"]
        hit_or_low = dataframe["close"] <= dataframe["or_low"]
        dataframe.loc[hit_or_high.fillna(False), "exit_long"] = 1
        dataframe.loc[hit_or_low.fillna(False), "exit_short"] = 1
        return dataframe


class PatternScalpStrongStrategy(PatternScalpStrategy):
    """Same rules but only ≥50% daily ATR flushes (closer to video preference)."""

    prefer_strong_manip = True
    strong_manip_atr_frac = 0.50
    stoploss = -0.025
    minimal_roi = {"0": 0.03, "60": 0.018, "180": 0.01, "360": 0}
