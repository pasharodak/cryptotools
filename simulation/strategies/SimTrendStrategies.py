"""Trend / breakout / liquidity / channel strategies for sim player (5m + 4h resample)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import talib.abstract as ta
from freqtrade.strategy.strategy_helper import merge_informative_pair
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase  # noqa: E402


def _resample_4h(dataframe: DataFrame) -> DataFrame:
    df = dataframe.set_index("date")
    ohlc = df.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return ohlc.dropna(subset=["close"]).reset_index()


def _merge_4h(dataframe: DataFrame, informative: DataFrame) -> DataFrame:
    return merge_informative_pair(dataframe, informative, "5m", "4h", ffill=True)


class SimEmaGoldenCross(_LiteBase):
    """EMA 50/200 golden cross on 4H (resampled), retest EMA50 on 5m, ADX>22."""

    stoploss = -0.025
    minimal_roi = {"0": 0.04, "240": 0.02, "720": 0.01, "1440": 0}
    pair_cooldown_minutes = 720
    startup_candle_count = 250
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        inf = _resample_4h(dataframe)
        inf["ema50"] = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["adx"] = ta.ADX(inf, timeperiod=14)
        inf["golden"] = (inf["ema50"] > inf["ema200"]).astype(int)
        dataframe = _merge_4h(dataframe, inf)

        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50_m"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        trend_up = (dataframe["golden_4h"] == 1) & (dataframe["adx_4h"] > 22)
        trend_dn = (dataframe["golden_4h"] == 0) & (dataframe["adx_4h"] > 22)
        retest_long = (
            (dataframe["close"] > dataframe["ema50_4h"])
            & (dataframe["low"] <= dataframe["ema50_m"] * 1.003)
            & (dataframe["close"] > dataframe["ema50_m"])
        )
        retest_short = (
            (dataframe["close"] < dataframe["ema50_4h"])
            & (dataframe["high"] >= dataframe["ema50_m"] * 0.997)
            & (dataframe["close"] < dataframe["ema50_m"])
        )
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        dataframe.loc[trend_up & retest_long & vol, "enter_long"] = 1
        dataframe.loc[trend_dn & retest_short & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["close"], dataframe["ema20"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["close"], dataframe["ema20"]), "exit_short"] = 1
        dataframe.loc[dataframe["golden_4h"] == 0, "exit_long"] = 1
        dataframe.loc[dataframe["golden_4h"] == 1, "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimBreakoutRetest(_LiteBase):
    """Range breakout with volume, retest zone entry."""

    stoploss = -0.022
    minimal_roi = {"0": 0.03, "180": 0.015, "480": 0.008, "960": 0}
    pair_cooldown_minutes = 480
    startup_candle_count = 200
    range_lookback = 96
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        lb = self.range_lookback
        dataframe["range_high"] = dataframe["high"].rolling(lb).max()
        dataframe["range_low"] = dataframe["low"].rolling(lb).min()
        dataframe["range_h"] = dataframe["range_high"] - dataframe["range_low"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
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
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        brk_up = (dataframe["close"] > dataframe["range_high"].shift(1)) & (
            dataframe["volume"] > dataframe["vol_sma"] * 1.35
        )
        brk_dn = (dataframe["close"] < dataframe["range_low"].shift(1)) & (
            dataframe["volume"] > dataframe["vol_sma"] * 1.35
        )
        dataframe["had_brk_up"] = brk_up.rolling(48).max().fillna(0)
        dataframe["had_brk_dn"] = brk_dn.rolling(48).max().fillna(0)
        retest_long = (
            (dataframe["had_brk_up"] > 0)
            & (dataframe["low"] <= dataframe["range_high"].shift(1) * 1.005)
            & (dataframe["close"] > dataframe["range_high"].shift(1))
            & (dataframe["bull_engulf"] | (dataframe["close"] > dataframe["open"]))
        )
        retest_short = (
            (dataframe["had_brk_dn"] > 0)
            & (dataframe["high"] >= dataframe["range_low"].shift(1) * 0.995)
            & (dataframe["close"] < dataframe["range_low"].shift(1))
            & (dataframe["bear_engulf"] | (dataframe["close"] < dataframe["open"]))
        )
        trend_ok = dataframe["adx"].between(18, 40)
        dataframe.loc[trend_ok & retest_long, "enter_long"] = 1
        dataframe.loc[trend_ok & retest_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        tp_long = dataframe["close"] >= dataframe["range_high"] + dataframe["range_h"]
        tp_short = dataframe["close"] <= dataframe["range_low"] - dataframe["range_h"]
        dataframe.loc[tp_long, "exit_long"] = 1
        dataframe.loc[tp_short, "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimFibPullback(_LiteBase):
    """Fib 0.618–0.786 pullback in 4H uptrend; DCA entries."""

    stoploss = -0.03
    minimal_roi = {"0": 0.035, "360": 0.02, "720": 0.01, "1440": 0}
    pair_cooldown_minutes = 600
    startup_candle_count = 250
    max_entry_position_adjustment = 2
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        inf = _resample_4h(dataframe)
        inf["ema50"] = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["swing_high"] = inf["high"].rolling(30).max()
        inf["swing_low"] = inf["low"].rolling(30).min()
        inf["fib_618"] = inf["swing_high"] - 0.618 * (inf["swing_high"] - inf["swing_low"])
        inf["fib_786"] = inf["swing_high"] - 0.786 * (inf["swing_high"] - inf["swing_low"])
        inf["uptrend"] = (inf["ema50"] > inf["ema200"]).astype(int)
        inf["downtrend"] = (inf["ema50"] < inf["ema200"]).astype(int)
        dataframe = _merge_4h(dataframe, inf)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        in_zone_long = dataframe["low"].between(
            dataframe["fib_786_4h"] * 0.998, dataframe["fib_618_4h"] * 1.002
        )
        in_zone_short = dataframe["high"].between(
            dataframe["fib_618_4h"] * 0.998, dataframe["fib_786_4h"] * 1.002
        )
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.8
        dataframe.loc[(dataframe["uptrend_4h"] == 1) & in_zone_long & vol, "enter_long"] = 1
        dataframe.loc[(dataframe["downtrend_4h"] == 1) & in_zone_short & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["swing_high_4h"] * 0.995, "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["swing_low_4h"] * 1.005, "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimLiquiditySweep(_LiteBase):
    """Liquidity sweep below swing low + pin bar + volume spike."""

    stoploss = -0.018
    minimal_roi = {"0": 0.025, "90": 0.012, "240": 0.006, "480": 0}
    pair_cooldown_minutes = 360
    startup_candle_count = 120
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["swing_low"] = dataframe["low"].rolling(36).min()
        dataframe["swing_high"] = dataframe["high"].rolling(36).max()
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["body"] = (dataframe["close"] - dataframe["open"]).abs()
        dataframe["lower_wick"] = dataframe[["open", "close"]].min(axis=1) - dataframe["low"]
        dataframe["upper_wick"] = dataframe["high"] - dataframe[["open", "close"]].max(axis=1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        sweep_long = (dataframe["low"] < dataframe["swing_low"].shift(1)) & (
            dataframe["close"] > dataframe["swing_low"].shift(1)
        )
        sweep_short = (dataframe["high"] > dataframe["swing_high"].shift(1)) & (
            dataframe["close"] < dataframe["swing_high"].shift(1)
        )
        pin_long = (dataframe["lower_wick"] > dataframe["body"] * 1.5) & (dataframe["close"] > dataframe["open"])
        pin_short = (dataframe["upper_wick"] > dataframe["body"] * 1.5) & (dataframe["close"] < dataframe["open"])
        vol_spike = dataframe["volume"] > dataframe["vol_sma"] * 1.5
        dataframe.loc[sweep_long & pin_long & vol_spike, "enter_long"] = 1
        dataframe.loc[sweep_short & pin_short & vol_spike, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["swing_high"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["swing_low"], "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimChannelMeanRev(_LiteBase):
    """Horizontal channel mean-reversion: RSI 30/70 + ADX<20."""

    stoploss = -0.02
    minimal_roi = {"0": 0.02, "60": 0.012, "180": 0.006, "480": 0}
    pair_cooldown_minutes = 300
    startup_candle_count = 150
    channel_lookback = 72
    sim_leverage = 1.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        lb = self.channel_lookback
        dataframe["ch_high"] = dataframe["high"].rolling(lb).max()
        dataframe["ch_low"] = dataframe["low"].rolling(lb).min()
        dataframe["ch_mid"] = (dataframe["ch_high"] + dataframe["ch_low"]) / 2
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        touches_hi = (dataframe["high"] >= dataframe["ch_high"] * 0.998).rolling(12).sum()
        touches_lo = (dataframe["low"] <= dataframe["ch_low"] * 1.002).rolling(12).sum()
        dataframe["channel_ok"] = (touches_hi >= 2) & (touches_lo >= 2)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        flat = (dataframe["adx"] < 20) & dataframe["channel_ok"]
        at_low = dataframe["low"] <= dataframe["ch_low"] * 1.003
        at_high = dataframe["high"] >= dataframe["ch_high"] * 0.997
        dataframe.loc[flat & at_low & (dataframe["rsi"] < 32), "enter_long"] = 1
        dataframe.loc[flat & at_high & (dataframe["rsi"] > 68), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["ch_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["ch_mid"], "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)
