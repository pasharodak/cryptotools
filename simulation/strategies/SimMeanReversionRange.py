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
