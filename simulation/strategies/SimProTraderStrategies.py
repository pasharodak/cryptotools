# Pro-trader playbook — Okala 80/20, Clement EMA momentum, Jason confluence, Marc filter.
"""Four rule-based strategies adapted from known discretionary approaches to crypto 5m.

TF mapping (project standard is 5m):
- Okala 10m structure → rolling range on ~12 bars (~1h) + 5m reaction entry
- Clement 15m/1h → 5m EMA stack; flat before UTC day end
- Jason weekly→1h → 4h direction + 5m confluence entry
- Marc HTF filter → 4h trend + 5m reasons score (≥3 of 5)
"""

from __future__ import annotations

import numpy as np
import talib.abstract as ta
from ctengine.strategy.strategy_helper import merge_informative_pair
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


def _resample_tf(dataframe: DataFrame, rule: str) -> DataFrame:
    df = dataframe.set_index("date")
    ohlc = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return ohlc.dropna(subset=["close"]).reset_index()


def _merge_tf(dataframe: DataFrame, informative: DataFrame, inf_tf: str) -> DataFrame:
    return merge_informative_pair(dataframe, informative, "5m", inf_tf, ffill=True)


def _ensure_utc_hour(df: DataFrame) -> DataFrame:
    if "date" not in df.columns:
        return df
    dt = df["date"]
    try:
        if getattr(dt.dt, "tz", None) is None:
            hours = dt.dt.hour
        else:
            hours = dt.dt.tz_convert("UTC").dt.hour
    except (TypeError, AttributeError, ValueError):
        hours = dt.dt.hour
    df = df.copy()
    df["utc_hour"] = hours
    return df


class OkalaRange8020Strategy(_LiteBase):
    """Okala «80/20»: reaction at 20%/80% of recent range — rejection or continuation.

    Structure from rolling range (≈10m idea on 5m data). Entry on 5m wick/close
    reaction. Tight structural stop beyond the reaction wick.
    """

    stoploss = -0.004
    minimal_roi = {"0": 0.015, "40": 0.008, "120": 0}
    pair_cooldown_minutes = 90
    startup_candle_count = 80
    range_lookback = 24  # ~2h on 5m — stable range for 20/80 levels
    zone_tol = 0.0025

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        lb = self.range_lookback
        rh = dataframe["high"].rolling(lb).max()
        rl = dataframe["low"].rolling(lb).min()
        span = (rh - rl).replace(0, np.nan)
        dataframe["rng_high"] = rh
        dataframe["rng_low"] = rl
        dataframe["lvl_80"] = rl + 0.80 * span
        dataframe["lvl_20"] = rl + 0.20 * span
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        body = (dataframe["close"] - dataframe["open"]).abs()
        dataframe["lower_wick"] = dataframe[["open", "close"]].min(axis=1) - dataframe["low"]
        dataframe["upper_wick"] = dataframe["high"] - dataframe[["open", "close"]].max(axis=1)
        dataframe["body"] = body
        # Prior range (shift 1) so current bar reacts to established levels
        dataframe["lvl_80_p"] = dataframe["lvl_80"].shift(1)
        dataframe["lvl_20_p"] = dataframe["lvl_20"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        tol = self.zone_tol
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        l80 = dataframe["lvl_80_p"]
        l20 = dataframe["lvl_20_p"]

        # Rejection short at 80%: pierce above, close back below
        rej_short = (
            (dataframe["high"] >= l80 * (1 - tol))
            & (dataframe["close"] < l80)
            & (dataframe["upper_wick"] > dataframe["body"] * 1.2)
            & (dataframe["close"] < dataframe["open"])
        )
        # Rejection long at 20%: pierce below, close back above
        rej_long = (
            (dataframe["low"] <= l20 * (1 + tol))
            & (dataframe["close"] > l20)
            & (dataframe["lower_wick"] > dataframe["body"] * 1.2)
            & (dataframe["close"] > dataframe["open"])
        )
        # Continuation long: decisive close through 80%
        cont_long = (
            qtpylib.crossed_above(dataframe["close"], l80)
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["volume"] > dataframe["vol_sma"] * 1.15)
        )
        # Continuation short: decisive close through 20%
        cont_short = (
            qtpylib.crossed_below(dataframe["close"], l20)
            & (dataframe["close"] < dataframe["open"])
            & (dataframe["volume"] > dataframe["vol_sma"] * 1.15)
        )

        dataframe.loc[vol & (rej_long | cont_long), "enter_long"] = 1
        dataframe.loc[vol & (rej_short | cont_short), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid = (dataframe["rng_high"] + dataframe["rng_low"]) / 2.0
        dataframe.loc[dataframe["close"] < mid, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > mid, "exit_short"] = 1
        return dataframe


class ClementEmaMomentumStrategy(_LiteBase):
    """Clement Ang: EMA 9/21/50 aligned → buy/sell pullback to EMA21.

    Mechanical only. SL ≈ 1.5×ATR (hard floor), TP ≈ 2.5R via ROI.
    Trail after ~1R. Flat before UTC 22:00 (no overnight).
    """

    stoploss = -0.012
    minimal_roi = {"0": 0.027, "60": 0.014, "180": 0}  # ~2.5 × 1.1% typical ATR stop
    pair_cooldown_minutes = 120
    startup_candle_count = 80
    atr_sl_mult = 1.5
    trail_after_r = 1.0
    flat_after_utc_hour = 22

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe = _ensure_utc_hour(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        session = dataframe["utc_hour"] < self.flat_after_utc_hour
        bull = (dataframe["ema9"] > dataframe["ema21"]) & (dataframe["ema21"] > dataframe["ema50"])
        bear = (dataframe["ema9"] < dataframe["ema21"]) & (dataframe["ema21"] < dataframe["ema50"])
        # Pullback to 21 EMA (Clement), not to 9
        long_pb = (
            bull
            & (dataframe["low"] <= dataframe["ema21"] * 1.002)
            & (dataframe["close"] > dataframe["ema21"])
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["rsi"] > 42)
            & (dataframe["rsi"] < 68)
        )
        short_pb = (
            bear
            & (dataframe["high"] >= dataframe["ema21"] * 0.998)
            & (dataframe["close"] < dataframe["ema21"])
            & (dataframe["close"] < dataframe["open"])
            & (dataframe["rsi"] < 58)
            & (dataframe["rsi"] > 32)
        )
        # Skip tiny ATR (noise) and huge ATR (gap risk)
        atr_ok = dataframe["atr_pct"].between(0.002, 0.025)
        dataframe.loc[session & vol & atr_ok & long_pb, "enter_long"] = 1
        dataframe.loc[session & vol & atr_ok & short_pb, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Stack break or end-of-day flatten
        dataframe.loc[
            (dataframe["close"] < dataframe["ema21"]) | (dataframe["utc_hour"] >= self.flat_after_utc_hour),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["close"] > dataframe["ema21"]) | (dataframe["utc_hour"] >= self.flat_after_utc_hour),
            "exit_short",
        ] = 1
        return dataframe

    def custom_stoploss_from_ohlcv(
        self,
        dataframe: DataFrame,
        trade,
        current_rate: float,
        current_profit: float,
    ) -> float | None:
        """1.5×ATR initial; after +1R trail to ~breakeven+."""
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        atr = last.get("atr")
        close = float(last.get("close") or current_rate or 0)
        if atr is None or close <= 0:
            return None
        atr = float(atr)
        r = self.atr_sl_mult * atr / close
        if r <= 0:
            return None
        if current_profit >= self.trail_after_r * r:
            # Lock ~0.3R after 1R move
            return max(-0.003, -(0.3 * r))
        return -min(abs(self.stoploss), r)


class JasonConfluenceStrategy(_LiteBase):
    """Jason Sen confluence: HTF trend + Fib zone + MA + swing-line in one price area.

    Direction from 4h EMA50/200. Entry on 5m only when ≥3 confluence factors
    overlap near price. Structural stop beyond swing that invalidates the idea.
    """

    stoploss = -0.014
    minimal_roi = {"0": 0.04, "180": 0.02, "480": 0.01, "960": 0}  # ≥2R
    pair_cooldown_minutes = 240
    startup_candle_count = 250

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        inf = _resample_tf(dataframe, "4h")
        inf["ema50"] = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["swing_high"] = inf["high"].rolling(20).max()
        inf["swing_low"] = inf["low"].rolling(20).min()
        span = (inf["swing_high"] - inf["swing_low"]).replace(0, np.nan)
        inf["fib_618"] = inf["swing_high"] - 0.618 * span
        inf["fib_500"] = inf["swing_high"] - 0.500 * span
        # Rising/falling swing line proxy (linreg of closes)
        inf["linreg"] = ta.LINEARREG(inf, timeperiod=20)
        inf["uptrend"] = ((inf["ema50"] > inf["ema200"]) & (inf["close"] > inf["ema50"])).astype(int)
        inf["downtrend"] = ((inf["ema50"] < inf["ema200"]) & (inf["close"] < inf["ema50"])).astype(int)
        dataframe = _merge_tf(dataframe, inf, "4h")

        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50_m"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["swing_lo_5m"] = dataframe["low"].rolling(36).min()
        dataframe["swing_hi_5m"] = dataframe["high"].rolling(36).max()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        # Factor proximity (±0.35%)
        near = 0.0035
        near_fib = (
            (dataframe["close"] - dataframe["fib_618_4h"]).abs() / dataframe["close"] < near
        ) | ((dataframe["close"] - dataframe["fib_500_4h"]).abs() / dataframe["close"] < near)
        near_ma = (
            (dataframe["close"] - dataframe["ema21"]).abs() / dataframe["close"] < near
        ) | ((dataframe["close"] - dataframe["ema50_m"]).abs() / dataframe["close"] < near)
        near_line = (dataframe["close"] - dataframe["linreg_4h"]).abs() / dataframe["close"] < near
        near_htf_ema = (dataframe["close"] - dataframe["ema50_4h"]).abs() / dataframe["close"] < near * 1.5

        factors_long = (
            near_fib.astype(int) + near_ma.astype(int) + near_line.astype(int) + near_htf_ema.astype(int)
        )
        factors_short = factors_long  # same zones; direction from HTF

        bounce_long = (dataframe["low"] <= dataframe["ema21"] * 1.002) & (
            dataframe["close"] > dataframe["ema21"]
        ) & (dataframe["close"] > dataframe["open"])
        bounce_short = (dataframe["high"] >= dataframe["ema21"] * 0.998) & (
            dataframe["close"] < dataframe["ema21"]
        ) & (dataframe["close"] < dataframe["open"])

        dataframe.loc[
            (dataframe["uptrend_4h"] == 1) & (factors_long >= 3) & bounce_long & vol,
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["downtrend_4h"] == 1) & (factors_short >= 3) & bounce_short & vol,
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] < dataframe["swing_lo_5m"]) | (dataframe["uptrend_4h"] == 0),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["close"] > dataframe["swing_hi_5m"]) | (dataframe["downtrend_4h"] == 0),
            "exit_short",
        ] = 1
        return dataframe


class MarcSetupFilterStrategy(_LiteBase):
    """Marc Walton: trade only when ≥3 of 5 independent reasons agree.

    Reasons: (1) HTF trend 4h, (2) fresh level, (3) momentum, (4) candle pattern,
    (5) BTC alignment (DXY/index proxy for crypto). Stop fixed before entry — never widened.
    """

    stoploss = -0.009
    minimal_roi = {"0": 0.03, "120": 0.015, "360": 0.008, "720": 0}  # ≥2R
    pair_cooldown_minutes = 180
    startup_candle_count = 220
    min_reasons = 3

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        inf = _resample_tf(dataframe, "4h")
        inf["ema50"] = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["adx"] = ta.ADX(inf, timeperiod=14)
        inf["uptrend"] = ((inf["ema50"] > inf["ema200"]) & (inf["adx"] > 18)).astype(int)
        inf["downtrend"] = ((inf["ema50"] < inf["ema200"]) & (inf["adx"] > 18)).astype(int)
        dataframe = _merge_tf(dataframe, inf, "4h")

        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50_m"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["swing_lo"] = dataframe["low"].rolling(48).min()
        dataframe["swing_hi"] = dataframe["high"].rolling(48).max()
        # Fresh level: within 0.4% of prior swing (not including current bar)
        dataframe["near_sup"] = (
            (dataframe["low"] - dataframe["swing_lo"].shift(1)).abs() / dataframe["close"] < 0.004
        )
        dataframe["near_res"] = (
            (dataframe["high"] - dataframe["swing_hi"].shift(1)).abs() / dataframe["close"] < 0.004
        )
        body = (dataframe["close"] - dataframe["open"]).abs()
        dataframe["bull_engulf"] = (
            (dataframe["close"] > dataframe["open"])
            & (dataframe["close"] > dataframe["open"].shift(1))
            & (dataframe["open"] <= dataframe["close"].shift(1))
            & (body > body.shift(1))
        )
        dataframe["bear_engulf"] = (
            (dataframe["close"] < dataframe["open"])
            & (dataframe["close"] < dataframe["open"].shift(1))
            & (dataframe["open"] >= dataframe["close"].shift(1))
            & (body > body.shift(1))
        )
        lower_wick = dataframe[["open", "close"]].min(axis=1) - dataframe["low"]
        upper_wick = dataframe["high"] - dataframe[["open", "close"]].max(axis=1)
        dataframe["pin_long"] = (lower_wick > body * 1.5) & (dataframe["close"] > dataframe["open"])
        dataframe["pin_short"] = (upper_wick > body * 1.5) & (dataframe["close"] < dataframe["open"])

        # BTC alignment (DXY/index stand-in)
        dataframe["btc_bull"] = 0
        dataframe["btc_bear"] = 0
        pair = (metadata or {}).get("pair") or ""
        if pair != "BTC/USDT:USDT" and self.dp is not None:
            try:
                btc = self.dp.get_pair_dataframe(pair="BTC/USDT:USDT", timeframe=self.timeframe)
                if btc is not None and not btc.empty:
                    btc = btc[["date", "close"]].copy()
                    btc["btc_ema21"] = ta.EMA(btc["close"], timeperiod=21)
                    btc["btc_ema50"] = ta.EMA(btc["close"], timeperiod=50)
                    dataframe = merge_informative_pair(
                        dataframe, btc, self.timeframe, self.timeframe, ffill=True
                    )
            except Exception:
                pass
        ema21_c = "btc_ema21_5m" if "btc_ema21_5m" in dataframe.columns else "btc_ema21"
        ema50_c = "btc_ema50_5m" if "btc_ema50_5m" in dataframe.columns else "btc_ema50"
        if ema21_c in dataframe.columns and ema50_c in dataframe.columns:
            dataframe["btc_bull"] = (dataframe[ema21_c] > dataframe[ema50_c]).astype(int)
            dataframe["btc_bear"] = (dataframe[ema21_c] < dataframe[ema50_c]).astype(int)
        elif pair == "BTC/USDT:USDT":
            dataframe["btc_bull"] = (dataframe["ema21"] > dataframe["ema50_m"]).astype(int)
            dataframe["btc_bear"] = (dataframe["ema21"] < dataframe["ema50_m"]).astype(int)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # (1) HTF trend
        r1_long = dataframe["uptrend_4h"] == 1
        r1_short = dataframe["downtrend_4h"] == 1
        # (2) Fresh level
        r2_long = dataframe["near_sup"]
        r2_short = dataframe["near_res"]
        # (3) Momentum
        r3_long = (dataframe["adx"] > 20) & (dataframe["macdhist"] > 0) & (
            dataframe["macdhist"] > dataframe["macdhist"].shift(1)
        )
        r3_short = (dataframe["adx"] > 20) & (dataframe["macdhist"] < 0) & (
            dataframe["macdhist"] < dataframe["macdhist"].shift(1)
        )
        # (4) Pattern
        r4_long = dataframe["bull_engulf"] | dataframe["pin_long"]
        r4_short = dataframe["bear_engulf"] | dataframe["pin_short"]
        # (5) BTC / market correlation
        r5_long = dataframe["btc_bull"] == 1
        r5_short = dataframe["btc_bear"] == 1

        score_long = (
            r1_long.astype(int)
            + r2_long.astype(int)
            + r3_long.astype(int)
            + r4_long.astype(int)
            + r5_long.astype(int)
        )
        score_short = (
            r1_short.astype(int)
            + r2_short.astype(int)
            + r3_short.astype(int)
            + r4_short.astype(int)
            + r5_short.astype(int)
        )
        dataframe["marc_score_long"] = score_long
        dataframe["marc_score_short"] = score_short

        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.8
        # Ideal (4–5) or normal (3) — same entry; risk sizing is discretionary in live
        ok_long = (score_long >= self.min_reasons) & r1_long & vol
        ok_short = (score_short >= self.min_reasons) & r1_short & vol
        dataframe.loc[ok_long, "enter_long"] = 1
        dataframe.loc[ok_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["uptrend_4h"] == 0) | (dataframe["close"] < dataframe["ema21"]),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["downtrend_4h"] == 0) | (dataframe["close"] > dataframe["ema21"]),
            "exit_short",
        ] = 1
        return dataframe
