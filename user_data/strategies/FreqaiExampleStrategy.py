import logging
from functools import reduce

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import IStrategy


logger = logging.getLogger(__name__)

# Minimum model confidence to enter (classifier probability).
PROB_THRESHOLD = 0.55
# Minimum future move to label training sample as up/down (0.3%).
MOVE_THRESHOLD = 0.003


class FreqaiExampleStrategy(IStrategy):
    """FreqAI: LightGBM classifier (up/down) with probability filter; Bybit futures."""

    minimal_roi = {"0": 0.04, "60": 0.015, "180": 0.005, "360": 0}

    plot_config = {
        "main_plot": {},
        "subplots": {
            "&s-up_or_down": {"&s-up_or_down": {"color": "blue"}},
            "do_predict": {"do_predict": {"color": "brown"}},
        },
    }

    process_only_new_candles = True
    stoploss = -0.05
    use_exit_signal = True
    startup_candle_count: int = 500
    can_short = True

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-sma-period"] = ta.SMA(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.2
        )
        dataframe["bb_lowerband-period"] = bollinger["lower"]
        dataframe["bb_middleband-period"] = bollinger["mid"]
        dataframe["bb_upperband-period"] = bollinger["upper"]

        dataframe["%-bb_width-period"] = (
            dataframe["bb_upperband-period"] - dataframe["bb_lowerband-period"]
        ) / dataframe["bb_middleband-period"]
        dataframe["%-close-bb_lower-period"] = dataframe["close"] / dataframe["bb_lowerband-period"]
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        dataframe["%-relative_volume-period"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        label = self.freqai_info["feature_parameters"]["label_period_candles"]
        future_return = dataframe["close"].shift(-label) / dataframe["close"] - 1

        self.freqai.class_names = ["down", "up"]
        dataframe["&s-up_or_down"] = np.where(
            future_return > MOVE_THRESHOLD,
            "up",
            "down",
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        has_prob = "up" in df.columns and "down" in df.columns

        if has_prob:
            enter_long_conditions = [
                df["do_predict"] == 1,
                df["up"] > PROB_THRESHOLD,
            ]
            enter_short_conditions = [
                df["do_predict"] == 1,
                df["down"] > PROB_THRESHOLD,
            ]
        else:
            enter_long_conditions = [
                df["do_predict"] == 1,
                df["&s-up_or_down"] == "up",
            ]
            enter_short_conditions = [
                df["do_predict"] == 1,
                df["&s-up_or_down"] == "down",
            ]

        if enter_long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_long_conditions), ["enter_long", "enter_tag"]
            ] = (1, "freqai_up")

        if enter_short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_short_conditions), ["enter_short", "enter_tag"]
            ] = (1, "freqai_down")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        has_prob = "up" in df.columns and "down" in df.columns

        if has_prob:
            exit_long_conditions = [df["do_predict"] == 1, df["down"] > PROB_THRESHOLD]
            exit_short_conditions = [df["do_predict"] == 1, df["up"] > PROB_THRESHOLD]
        else:
            exit_long_conditions = [df["do_predict"] == 1, df["&s-up_or_down"] == "down"]
            exit_short_conditions = [df["do_predict"] == 1, df["&s-up_or_down"] == "up"]

        if exit_long_conditions:
            df.loc[reduce(lambda x, y: x & y, exit_long_conditions), "exit_long"] = 1

        if exit_short_conditions:
            df.loc[reduce(lambda x, y: x & y, exit_short_conditions), "exit_short"] = 1

        return df

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
        return min(3.0, max_leverage)
