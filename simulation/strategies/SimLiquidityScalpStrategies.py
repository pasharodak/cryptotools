# Liquidity / orderflow-inspired scalp patterns (candle proxy for DOM ideas).
"""Hash-Hedge-style ideas without L2: active-alt breakout + liquidity sweep reclaim.

Inspired by live-stream scalping notes:
- trade active alts (volume + ATR "pulse"), not heavy BTC/ETH style chop
- breakout after level forms + volume impulse
- "прострел": wick beyond swing (liquidity grab) then close back / reclaim
- avoid mid-range "death zone" for breakouts; wait for a clean setup
"""

from __future__ import annotations

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class AltVolumeBreakoutStrategy(_LiteBase):
    """Active-coin Donchian breakout with volume/ATR pulse filter."""

    stoploss = -0.015
    minimal_roi = {"0": 0.012, "25": 0.006, "75": 0}
    pair_cooldown_minutes = 45
    startup_candle_count = 80

    # Tunables (candle proxy for screener + impulse)
    don_period = 12
    vol_mult = 1.6
    min_atr_pct = 0.0035  # ~0.35% ATR/close — skip dead coins
    max_atr_pct = 0.035  # skip chaos prints

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p = self.don_period
        dataframe["don_high"] = dataframe["high"].rolling(p).max().shift(1)
        dataframe["don_low"] = dataframe["low"].rolling(p).min().shift(1)
        dataframe["don_mid"] = (dataframe["don_high"] + dataframe["don_low"]) / 2.0
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"].replace(0, 1)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, 1)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        # Range position of prior close inside channel (0=low, 1=high)
        width = (dataframe["don_high"] - dataframe["don_low"]).replace(0, 1)
        dataframe["range_pos"] = (dataframe["close"].shift(1) - dataframe["don_low"]) / width
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        active = (dataframe["atr_pct"] >= self.min_atr_pct) & (dataframe["atr_pct"] <= self.max_atr_pct)
        vol = dataframe["vol_ratio"] >= self.vol_mult
        # Not already mid-channel: prefer break from upper/lower third (avoid death zone)
        from_upper = dataframe["range_pos"] >= 0.55
        from_lower = dataframe["range_pos"] <= 0.45

        break_up = dataframe["close"] > dataframe["don_high"]
        break_dn = dataframe["close"] < dataframe["don_low"]
        fresh_up = break_up & (dataframe["close"].shift(1) <= dataframe["don_high"])
        fresh_dn = break_dn & (dataframe["close"].shift(1) >= dataframe["don_low"])

        # Mild trend alignment so we don't fade every spike
        up_bias = dataframe["close"] >= dataframe["ema50"] * 0.995
        dn_bias = dataframe["close"] <= dataframe["ema50"] * 1.005

        dataframe.loc[fresh_up & vol & active & from_upper & up_bias, "enter_long"] = 1
        dataframe.loc[fresh_dn & vol & active & from_lower & dn_bias, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["don_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["don_mid"], "exit_short"] = 1
        return dataframe


class LiquiditySweepReclaimStrategy(_LiteBase):
    """Liquidity sweep (wick beyond swing) + reclaim close — candle proxy for 'прострел'."""

    stoploss = -0.012
    minimal_roi = {"0": 0.009, "20": 0.0045, "60": 0}
    pair_cooldown_minutes = 40
    startup_candle_count = 80

    swing_period = 20
    vol_mult = 1.25
    min_wick_pct = 0.0012  # sweep depth vs close
    min_atr_pct = 0.003
    max_atr_pct = 0.04

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        p = self.swing_period
        dataframe["swing_high"] = dataframe["high"].rolling(p).max().shift(1)
        dataframe["swing_low"] = dataframe["low"].rolling(p).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"].replace(0, 1)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, 1)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        # Wick metrics
        dataframe["lower_wick"] = (dataframe[["open", "close"]].min(axis=1) - dataframe["low"]).clip(lower=0)
        dataframe["upper_wick"] = (dataframe["high"] - dataframe[["open", "close"]].max(axis=1)).clip(lower=0)
        dataframe["lower_wick_pct"] = dataframe["lower_wick"] / dataframe["close"].replace(0, 1)
        dataframe["upper_wick_pct"] = dataframe["upper_wick"] / dataframe["close"].replace(0, 1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        active = (dataframe["atr_pct"] >= self.min_atr_pct) & (dataframe["atr_pct"] <= self.max_atr_pct)
        vol = dataframe["vol_ratio"] >= self.vol_mult

        # Sweep lows then reclaim (bullish прострел / liquidity grab)
        sweep_lo = dataframe["low"] < dataframe["swing_low"]
        reclaim_lo = dataframe["close"] > dataframe["swing_low"]
        wick_ok_lo = dataframe["lower_wick_pct"] >= self.min_wick_pct
        # Prefer close in upper half of candle (buyers won the bar)
        bull_body = dataframe["close"] >= (dataframe["low"] + dataframe["high"]) / 2.0

        # Sweep highs then reclaim (bearish)
        sweep_hi = dataframe["high"] > dataframe["swing_high"]
        reclaim_hi = dataframe["close"] < dataframe["swing_high"]
        wick_ok_hi = dataframe["upper_wick_pct"] >= self.min_wick_pct
        bear_body = dataframe["close"] <= (dataframe["low"] + dataframe["high"]) / 2.0

        dataframe.loc[
            sweep_lo & reclaim_lo & wick_ok_lo & bull_body & vol & active,
            "enter_long",
        ] = 1
        dataframe.loc[
            sweep_hi & reclaim_hi & wick_ok_hi & bear_body & vol & active,
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Soft exit on opposite EMA cross / reclaim fade
        dataframe.loc[qtpylib.crossed_below(dataframe["close"], dataframe["ema21"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["close"], dataframe["ema21"]), "exit_short"] = 1
        return dataframe


class AltVolumeBreakoutTestStrategy(AltVolumeBreakoutStrategy):
    """UI test clone of #31: 1x, SL -3%, no RSI/range chase, custom exit."""

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
        mid = last.get("don_mid")
        close = float(last.get("close") or current_rate or 0)
        if mid is None or close <= 0:
            return None
        mid = float(mid)
        if trade.is_short and close > mid:
            return "don_mid"
        if (not trade.is_short) and close < mid:
            return "don_mid"
        return None
