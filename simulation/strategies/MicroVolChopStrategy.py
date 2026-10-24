"""Micro-volatility chop bot — fee-aware mean-reversion on 1m (sim / player).

Idea: trade only pairs/regimes with continuous small oscillations where the
typical 1-bar ATR covers Bybit taker round-trip (~0.11%) with margin. Fade
stretches from a fast EMA midline; exit back to midline or on tight SL/time.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import talib.abstract as ta
from ctengine.persistence import Trade
from ctengine.strategy import IStrategy
from pandas import DataFrame
from technical import qtpylib


# Bybit USDT-perp taker ≈ 0.055% per side → 0.11% round-trip.
FEE_RT = 0.0011
# Require ATR% ≥ this multiple of fee RT before allowing entries.
MIN_ATR_FEE_MULT = 1.8
# Min edge to midline (beyond fees) before taking a fade.
MIN_EDGE_PCT = 0.0015


class MicroVolChopStrategy(IStrategy):
    """1m chop fade — separate micro-vol bot for simulation."""

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "1m"
    process_only_new_candles = True
    startup_candle_count = 120
    use_exit_signal = True
    # Fee-aware: TP > SL so ~50% WR can clear taker RT ~0.11%.
    stoploss = -0.0022
    minimal_roi = {"0": 0.0045, "15": 0.0030, "40": 0.0020, "90": 0}
    trailing_stop = False
    pair_cooldown_minutes = 30
    sim_leverage = 1.0

    # Tunables (also used by the 15s/30s pure-python runner)
    ema_period = 20
    atr_period = 14
    er_lookback = 40
    max_efficiency = 0.42
    max_adx = 32.0
    z_entry = 2.0
    stretch_atr_k = 1.0

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(float(self.sim_leverage), float(max_leverage))

    def _pair_in_cooldown(self, pair: str, current_time: datetime) -> bool:
        closed = Trade.get_trades_proxy(pair=pair, is_open=False)
        if not closed:
            return False
        last = max(closed, key=lambda t: t.close_date or datetime.min.replace(tzinfo=UTC))
        if not last.close_date:
            return False
        close_dt = last.close_date
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=UTC)
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=UTC)
        return (now - close_dt) < timedelta(minutes=self.pair_cooldown_minutes)

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        return not self._pair_in_cooldown(pair, current_time)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"].astype(float)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=self.ema_period)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_period)
        dataframe["atr_pct"] = dataframe["atr"] / close.replace(0, np.nan)
        hi = dataframe["high"].rolling(20).max()
        lo = dataframe["low"].rolling(20).min()
        dataframe["range_pct"] = (hi - lo) / close.replace(0, np.nan)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        dataframe["vol_sma"] = dataframe["volume"].rolling(30).mean()

        # Kaufman efficiency ratio over er_lookback bars (low = choppy path).
        net = (close - close.shift(self.er_lookback)).abs()
        path = close.diff().abs().rolling(self.er_lookback).sum()
        dataframe["er"] = (net / path.replace(0, np.nan)).fillna(1.0)

        # Z-score of distance from EMA (in ATR units).
        dist = close - dataframe["ema"]
        dataframe["z_atr"] = dist / dataframe["atr"].replace(0, np.nan)
        dataframe["edge_pct"] = dist.abs() / close.replace(0, np.nan)

        # Fee gate: recent swing (not 1-bar ATR) must cover taker RT with margin.
        dataframe["fee_ok"] = dataframe["range_pct"] >= (FEE_RT * MIN_ATR_FEE_MULT)
        dataframe["edge_ok"] = dataframe["edge_pct"] >= (FEE_RT + MIN_EDGE_PCT)

        above = close > dataframe["ema"]
        dataframe["ema_cross"] = (above != above.shift(1)).fillna(False).astype(float)
        dataframe["crosses_1h"] = dataframe["ema_cross"].rolling(60).sum()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = (
            (dataframe["er"] <= self.max_efficiency)
            & (dataframe["adx"] < self.max_adx)
            & (dataframe["fee_ok"])
            & (dataframe["crosses_1h"] >= 4)
        )
        liquid = dataframe["volume"] > dataframe["vol_sma"] * 0.6
        stretch = dataframe["edge_ok"] & (dataframe["z_atr"].abs() >= self.z_entry)
        atr_stretch = dataframe["edge_pct"] >= (dataframe["atr_pct"] * self.stretch_atr_k)

        long_sig = (
            chop
            & liquid
            & stretch
            & atr_stretch
            & (dataframe["z_atr"] <= -self.z_entry)
            & (dataframe["rsi"] < 40)
        )
        short_sig = (
            chop
            & liquid
            & stretch
            & atr_stretch
            & (dataframe["z_atr"] >= self.z_entry)
            & (dataframe["rsi"] > 60)
        )
        dataframe.loc[long_sig, "enter_long"] = 1
        dataframe.loc[short_sig, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Mean-reversion complete: price back through EMA.
        dataframe.loc[
            qtpylib.crossed_above(dataframe["close"], dataframe["ema"])
            | (dataframe["z_atr"] >= -0.15),
            "exit_long",
        ] = 1
        dataframe.loc[
            qtpylib.crossed_below(dataframe["close"], dataframe["ema"])
            | (dataframe["z_atr"] <= 0.15),
            "exit_short",
        ] = 1
        return dataframe
