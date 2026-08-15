"""BB mean-reversion for ranging markets (Vantixs / Superior-Trade template on 5m).

Entry: price at BB band + RSI extreme + ADX<25 + BB width > squeeze threshold.
Exit: revert to BB midline. Stop -2%.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from simulation.strategies.LiteFinanceStrategies import _LiteBase  # noqa: E402
import talib.abstract as ta  # noqa: E402
from pandas import DataFrame  # noqa: E402


class SimMeanReversionRange(_LiteBase):
    """Range mean-reversion — ADX<25, no BB squeeze, exit at midline."""

    stoploss = -0.02
    minimal_roi = {"0": 0.025, "60": 0.015, "180": 0.008, "720": 0}
    pair_cooldown_minutes = 360
    adx_max = 25
    rsi_long_max = 35
    rsi_short_min = 65
    bb_width_min = 0.04
    bb_width_max = 0.07
    sim_leverage = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._bb(dataframe)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_sma"] = dataframe["atr"].rolling(20).mean()
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ranging = (dataframe["adx"] < self.adx_max) & dataframe["bb_width"].between(
            self.bb_width_min, self.bb_width_max
        )
        calm_vol = dataframe["atr"] <= dataframe["atr_sma"] * 1.5
        vol_ok = dataframe["volume"] > dataframe["vol_sma"] * 0.75
        touch_long = dataframe["close"] <= dataframe["bb_lower"]
        touch_short = dataframe["close"] >= dataframe["bb_upper"]
        dataframe.loc[
            ranging & calm_vol & vol_ok & touch_long & (dataframe["rsi"] < self.rsi_long_max),
            "enter_long",
        ] = 1
        dataframe.loc[
            ranging & calm_vol & vol_ok & touch_short & (dataframe["rsi"] > self.rsi_short_min),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["bb_mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["bb_mid"], "exit_short"] = 1
        return dataframe

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag,
        side: str,
        **kwargs,
    ) -> float:
        return min(self.sim_leverage, max_leverage)


class BollingerRsiTestStrategy(SimMeanReversionRange):
    """UI test clone of #33: 1x, SL -3%, no RSI/range chase, custom exit."""

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
        mid = last.get("bb_mid")
        close = float(last.get("close") or current_rate or 0)
        if mid is None or close <= 0:
            return None
        mid = float(mid)
        if trade.is_short and close < mid:
            return "bb_mid"
        if (not trade.is_short) and close > mid:
            return "bb_mid"
        return None
