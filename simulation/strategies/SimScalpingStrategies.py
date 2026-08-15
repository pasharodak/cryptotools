# Popular 5m scalping styles for simulation comparison (fee-aware ROI/SL).
"""Five classic crypto scalping patterns used by retail traders."""

from __future__ import annotations

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from simulation.strategies.LiteFinanceStrategies import _LiteBase


class ScalpEmaCrossStrategy(_LiteBase):
    """#1 EMA 8/21 cross — classic momentum scalp with volume."""

    stoploss = -0.01
    minimal_roi = {"0": 0.008, "20": 0.004, "60": 0}
    pair_cooldown_minutes = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema8"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["spread"] = (dataframe["ema8"] - dataframe["ema21"]).abs() / dataframe["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.1
        regime = (dataframe["adx"] > 14) & (dataframe["adx"] < 40) & (dataframe["spread"] > 0.0008)
        up = qtpylib.crossed_above(dataframe["ema8"], dataframe["ema21"])
        dn = qtpylib.crossed_below(dataframe["ema8"], dataframe["ema21"])
        dataframe.loc[up & vol & regime, "enter_long"] = 1
        dataframe.loc[dn & vol & regime, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["ema8"], dataframe["ema21"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["ema8"], dataframe["ema21"]), "exit_short"] = 1
        return dataframe


class ScalpRsiReclaimStrategy(_LiteBase):
    """#2 RSI reclaim — fade extremes after RSI returns from oversold/overbought."""

    stoploss = -0.012
    minimal_roi = {"0": 0.009, "25": 0.0045, "70": 0}
    pair_cooldown_minutes = 75

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["rsi_prev"] = dataframe["rsi"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.95
        reclaim_up = (dataframe["rsi_prev"] < 28) & (dataframe["rsi"] >= 28) & (dataframe["rsi"] < 45)
        reclaim_dn = (dataframe["rsi_prev"] > 72) & (dataframe["rsi"] <= 72) & (dataframe["rsi"] > 55)
        dataframe.loc[reclaim_up & vol & (dataframe["close"] > dataframe["ema50"] * 0.985), "enter_long"] = 1
        dataframe.loc[reclaim_dn & vol & (dataframe["close"] < dataframe["ema50"] * 1.015), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 68, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 32, "exit_short"] = 1
        return dataframe


class ScalpBbBounceStrategy(_LiteBase):
    """#3 Bollinger bounce — fade outer band touches in non-trending chop."""

    stoploss = -0.011
    minimal_roi = {"0": 0.008, "30": 0.004, "80": 0}
    pair_cooldown_minutes = 90

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._bb(dataframe)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        chop = (dataframe["adx"] < 25) & (dataframe["bb_width"] > 0.012)
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.85
        touch_lo = dataframe["low"] <= dataframe["bb_lower"]
        touch_hi = dataframe["high"] >= dataframe["bb_upper"]
        dataframe.loc[chop & vol & touch_lo & (dataframe["rsi"] < 40), "enter_long"] = 1
        dataframe.loc[chop & vol & touch_hi & (dataframe["rsi"] > 60), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["bb_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["bb_mid"], "exit_short"] = 1
        return dataframe


class ScalpStochStrategy(_LiteBase):
    """#4 Stochastic cross — %K/%D flip in extreme zones (14,3,3)."""

    stoploss = -0.01
    minimal_roi = {"0": 0.0075, "20": 0.0035, "55": 0}
    pair_cooldown_minutes = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        stoch = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowd_period=3)
        dataframe["slowk"] = stoch["slowk"]
        dataframe["slowd"] = stoch["slowd"]
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        cross_up = qtpylib.crossed_above(dataframe["slowk"], dataframe["slowd"])
        cross_dn = qtpylib.crossed_below(dataframe["slowk"], dataframe["slowd"])
        long_zone = (dataframe["slowk"] < 25) & (dataframe["slowd"] < 30)
        short_zone = (dataframe["slowk"] > 75) & (dataframe["slowd"] > 70)
        dataframe.loc[cross_up & long_zone & vol, "enter_long"] = 1
        dataframe.loc[cross_dn & short_zone & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["slowk"] > 80, "exit_long"] = 1
        dataframe.loc[dataframe["slowk"] < 20, "exit_short"] = 1
        return dataframe


class ScalpMacdHistStrategy(_LiteBase):
    """#5 MACD histogram flip — hist crosses zero with EMA trend filter."""

    stoploss = -0.012
    minimal_roi = {"0": 0.009, "30": 0.0045, "90": 0}
    pair_cooldown_minutes = 75

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.05
        hist_up = qtpylib.crossed_above(dataframe["macdhist"], 0)
        hist_dn = qtpylib.crossed_below(dataframe["macdhist"], 0)
        uptrend = dataframe["close"] > dataframe["ema50"]
        dntrend = dataframe["close"] < dataframe["ema50"]
        dataframe.loc[hist_up & uptrend & vol, "enter_long"] = 1
        dataframe.loc[hist_dn & dntrend & vol, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["macdhist"], 0), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["macdhist"], 0), "exit_short"] = 1
        return dataframe


class ScalpEmaCrossTestStrategy(ScalpEmaCrossStrategy):
    """UI test clone of #4: 1x, SL -3%, no RSI/range chase, custom exit."""

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
        e8 = float(last.get("ema8") or 0)
        e21 = float(last.get("ema21") or 0)
        if e8 <= 0 or e21 <= 0:
            return None
        if trade.is_short and e8 > e21:
            return "ema_flip"
        if (not trade.is_short) and e8 < e21:
            return "ema_flip"
        return None
