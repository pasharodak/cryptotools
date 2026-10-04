"""Classic trend/momentum strategies for sim player (Supertrend, MACD+EMA, RSI+EMA)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase  # noqa: E402


class SimSupertrend(_LiteBase):
    """Supertrend flip + RSI filter (ATR-based trend following)."""

    stoploss = -0.025
    minimal_roi = {"0": 0.03, "120": 0.015, "360": 0.008, "720": 0}
    pair_cooldown_minutes = 360
    startup_candle_count = 80
    sim_leverage = 3.0
    supertrend_period = 10
    supertrend_multiplier = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr = ta.ATR(dataframe, timeperiod=self.supertrend_period)
        hl2 = (dataframe["high"] + dataframe["low"]) / 2
        upper = hl2 + (self.supertrend_multiplier * atr)
        lower = hl2 - (self.supertrend_multiplier * atr)

        uptrend = np.ones(len(dataframe), dtype=bool)
        st = lower.copy()
        for i in range(1, len(dataframe)):
            if dataframe["close"].iloc[i] > upper.iloc[i - 1]:
                uptrend[i] = True
            elif dataframe["close"].iloc[i] < lower.iloc[i - 1]:
                uptrend[i] = False
            else:
                uptrend[i] = uptrend[i - 1]
                if uptrend[i] and lower.iloc[i] < lower.iloc[i - 1]:
                    lower.iloc[i] = lower.iloc[i - 1]
                if not uptrend[i] and upper.iloc[i] > upper.iloc[i - 1]:
                    upper.iloc[i] = upper.iloc[i - 1]
            st.iloc[i] = lower.iloc[i] if uptrend[i] else upper.iloc[i]

        dataframe["supertrend"] = st
        dataframe["supertrend_up"] = uptrend
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(False)
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(False)
        dataframe.loc[up_flip & (dataframe["rsi"] > 45) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[down_flip & (dataframe["rsi"] < 55) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        down_flip = ~dataframe["supertrend_up"] & dataframe["supertrend_up"].shift(1).fillna(False)
        up_flip = dataframe["supertrend_up"] & ~dataframe["supertrend_up"].shift(1).fillna(False)
        dataframe.loc[down_flip, "exit_long"] = 1
        dataframe.loc[up_flip, "exit_short"] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimSupertrendTest(SimSupertrend):
    """Supertrend test block: long-only, strict anti-chase, ST-line dynamic SL.

    Live Aug 2026: big losses = exchange SL (~−2%) before custom_exit fired.
    Router reads custom_stoploss_from_ohlcv to trail stop at ST break/fade.
    """

    stoploss = -0.012
    minimal_roi = {"0": 0.016, "60": 0.012, "180": 0.006, "480": 0}
    pair_cooldown_minutes = 480
    sim_leverage = 1.0
    allow_short_entries = False
    rsi_long_max = 52
    rsi_short_min = 48
    range_lookback_1h = 12
    max_long_range_pos = 0.45
    min_short_range_pos = 0.55
    ema_trend_period = 50
    min_st_dist_pct = 0.001
    max_st_dist_pct = 0.004
    max_impulse_ret = 0.005
    min_ret_1h = -0.002
    vol_sma_period = 20
    max_vol_ratio = 2.0
    min_adx = 25.0
    # simeon big-loss hours Aug 26–29 (UTC)
    blocked_entry_hours_utc = (
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 23
    )
    st_fade_dist_pct = 0.015
    st_fade_min_loss = -0.002
    st_break_buffer_pct = 0.0005
    sl_st_break = -0.004
    sl_st_fade = -0.007
    sl_time_tighten = -0.010

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=self.ema_trend_period)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        dataframe["ret_1h"] = dataframe["close"] / dataframe["close"].shift(self.range_lookback_1h) - 1.0
        vol_sma = dataframe["volume"].rolling(self.vol_sma_period).mean().replace(0, np.nan)
        dataframe["vol_ratio"] = dataframe["volume"] / vol_sma
        st = dataframe["supertrend"].replace(0, np.nan)
        dataframe["st_dist_pct"] = (dataframe["close"] - st) / st
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        ret = dataframe["ret_1h"]
        vol_r = dataframe["vol_ratio"]
        adx = dataframe["adx"]
        st_dist = dataframe["st_dist_pct"]
        ema50 = dataframe["ema50"]
        plus_di = dataframe["plus_di"]
        minus_di = dataframe["minus_di"]

        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        chase_long = (ret >= self.max_impulse_ret) | (
            (ret >= self.max_impulse_ret * 0.6) & (vol_r >= self.max_vol_ratio)
        )
        chase_short = (ret <= -self.max_impulse_ret) | (
            (ret <= -self.max_impulse_ret * 0.6) & (vol_r >= self.max_vol_ratio)
        )
        weak = (adx < self.min_adx) | (adx < adx.shift(3))
        dump_hour = ret < self.min_ret_1h
        bad_flip_long = (st_dist < self.min_st_dist_pct) | (st_dist > self.max_st_dist_pct)
        bad_flip_short = (st_dist > -self.min_st_dist_pct) | (st_dist < -self.max_st_dist_pct)
        trend_long = (dataframe["close"] > ema50) & (ema50 > ema50.shift(3))
        trend_short = (dataframe["close"] < ema50) & (ema50 < ema50.shift(3))
        di_long = plus_di > minus_di
        di_short = minus_di > plus_di
        bull_bar = dataframe["close"] > dataframe["open"]

        dataframe.loc[
            late_long
            | chase_long
            | weak
            | dump_hour
            | bad_flip_long
            | (~trend_long.fillna(False))
            | (~di_long.fillna(False))
            | (~bull_bar.fillna(False)),
            "enter_long",
        ] = 0
        dataframe.loc[
            late_short
            | chase_short
            | weak
            | bad_flip_short
            | (~trend_short.fillna(False))
            | (~di_short.fillna(False)),
            "enter_short",
        ] = 0
        if not self.allow_short_entries:
            dataframe["enter_short"] = 0

        if "date" in dataframe.columns and self.blocked_entry_hours_utc:
            dt = dataframe["date"]
            try:
                hours = dt.dt.tz_convert("UTC").dt.hour
            except (TypeError, AttributeError, ValueError):
                hours = dt.dt.hour
            hot = hours.isin(list(self.blocked_entry_hours_utc))
            dataframe.loc[hot, "enter_long"] = 0
            dataframe.loc[hot, "enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def custom_stoploss_from_ohlcv(
        self,
        dataframe: DataFrame,
        trade,
        current_rate: float,
        current_profit: float,
    ) -> float | None:
        """Tighten exchange stop before hard SL when price loses ST support."""
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        st = last.get("supertrend")
        close = last.get("close")
        if st is None or close is None or not close:
            return None
        st_f, close_f = float(st), float(close)

        if trade.is_short:
            if close_f > st_f * (1.0 + self.st_break_buffer_pct):
                return self.sl_st_break
            dist = (st_f - close_f) / close_f
            if dist <= self.st_fade_dist_pct and current_profit <= self.st_fade_min_loss:
                return self.sl_st_fade
            if current_profit <= -0.008:
                return self.sl_time_tighten
            return None

        if close_f < st_f * (1.0 - self.st_break_buffer_pct):
            return self.sl_st_break
        dist = (close_f - st_f) / close_f
        if dist <= self.st_fade_dist_pct and current_profit <= self.st_fade_min_loss:
            return self.sl_st_fade
        if current_profit <= -0.008:
            return self.sl_time_tighten
        return None

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        up = bool(last.get("supertrend_up"))
        st = last.get("supertrend")
        close = last.get("close")
        if st is None or close is None or not close:
            return None
        st_f, close_f = float(st), float(close)
        open_rate = float(getattr(trade, "open_rate", 0) or 0)
        if open_rate <= 0:
            return None

        if trade.is_short:
            if up:
                return "st_flip"
            break_lvl = st_f * (1.0 + self.st_break_buffer_pct)
            if close_f > break_lvl:
                return "st_break"
            dist = (st_f - close_f) / close_f
            pnl = open_rate / current_rate - 1.0
            if dist <= self.st_fade_dist_pct and pnl <= self.st_fade_min_loss:
                return "st_fade"
            return None

        if not up:
            return "st_flip"
        break_lvl = st_f * (1.0 - self.st_break_buffer_pct)
        if close_f < break_lvl:
            return "st_break"
        dist = (close_f - st_f) / close_f
        pnl = current_rate / open_rate - 1.0
        if dist <= self.st_fade_dist_pct and pnl <= self.st_fade_min_loss:
            return "st_fade"
        if pnl <= -0.008 and dist <= self.st_fade_dist_pct * 1.5:
            return "st_fade"
        return None


class SimMacdEma(_LiteBase):
    """MACD cross in direction of EMA200 trend."""

    stoploss = -0.025
    minimal_roi = {"0": 0.03, "180": 0.015, "480": 0.008, "960": 0}
    pair_cooldown_minutes = 480
    startup_candle_count = 220
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
            & (dataframe["close"] > dataframe["ema200"])
            & (dataframe["macdhist"] > 0)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
            & (dataframe["close"] < dataframe["ema200"])
            & (dataframe["macdhist"] < 0)
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"]) | (dataframe["rsi"] > 75),
            "exit_long",
        ] = 1
        dataframe.loc[
            qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"]) | (dataframe["rsi"] < 25),
            "exit_short",
        ] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class SimRsiEmaCross(_LiteBase):
    """RSI oversold/overbought + EMA12/26 + BB filter (CriptoPairs-style)."""

    stoploss = -0.022
    minimal_roi = {"0": 0.025, "90": 0.012, "240": 0.006, "480": 0}
    pair_cooldown_minutes = 300
    startup_candle_count = 80
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        bb = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_upper"] = bb["upper"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] < 35)
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["rsi"] > 65)
            & (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["close"] < dataframe["bb_upper"])
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > 70) | (dataframe["ema_fast"] < dataframe["ema_slow"]),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["rsi"] < 30) | (dataframe["ema_fast"] > dataframe["ema_slow"]),
            "exit_short",
        ] = 1
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)


class MacdEmaTestStrategy(SimMacdEma):
    """MACD+EMA test: long-only, anti-chase, ADX filter (simeon Aug WR~31%)."""

    stoploss = -0.02
    minimal_roi = {"0": 0.02, "60": 0.012, "180": 0.006, "480": 0.0}
    pair_cooldown_minutes = 360
    sim_leverage = 1.0
    allow_short_entries = False
    rsi_long_max = 55
    rsi_short_min = 45
    range_lookback_1h = 12
    max_long_range_pos = 0.55
    min_short_range_pos = 0.45
    min_adx = 22.0
    max_impulse_ret = 0.008

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        dataframe["ret_1h"] = dataframe["close"] / dataframe["close"].shift(self.range_lookback_1h) - 1.0
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        ret = dataframe["ret_1h"]
        adx = dataframe["adx"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos) | (ret >= self.max_impulse_ret)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos) | (ret <= -self.max_impulse_ret)
        weak = adx < self.min_adx
        dataframe.loc[late_long | weak, "enter_long"] = 0
        dataframe.loc[late_short | weak, "enter_short"] = 0
        if not self.allow_short_entries:
            dataframe["enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {"pair": getattr(trade, "pair", "")})
        last = df.iloc[-1]
        macd = last.get("macd")
        sig = last.get("macdsignal")
        if macd is None or sig is None:
            return None
        macd, sig = float(macd), float(sig)
        if trade.is_short and macd > sig:
            return "macd_flip"
        if (not trade.is_short) and macd < sig:
            return "macd_flip"
        return None
