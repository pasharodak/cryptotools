import logging
from functools import reduce

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import IStrategy


logger = logging.getLogger(__name__)

PROB_THRESHOLD = 0.62
MOVE_THRESHOLD = 0.004
ADX_MIN = 22
PAIR_COOLDOWN_MINUTES = 60


class CriptoFreqaiHybridStrategy(IStrategy):
    """
    FreqAI hybrid: LightGBM classifier (1h/4h features) + ADX/EMA/RSI guards on 5m.
    Based on Freqtrade FreqaiExampleHybridStrategy, adapted for Bybit futures.
    """

    minimal_roi = {"0": 0.04, "60": 0.015, "180": 0.005, "360": 0}

    plot_config = {
        "main_plot": {"ema50": {"color": "orange"}},
        "subplots": {
            "adx": {"adx": {"color": "purple"}},
            "do_predict": {"do_predict": {"color": "brown"}},
        },
    }

    process_only_new_candles = True
    stoploss = -0.05
    use_exit_signal = True
    startup_candle_count: int = 500
    can_short = True

    protections = [
        {
            "method": "CooldownPeriod",
            "stop_duration": PAIR_COOLDOWN_MINUTES,
        }
    ]

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

        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)

        return dataframe

    def _freqai_long_signal(self, df: DataFrame) -> list:
        if "up" in df.columns and "down" in df.columns:
            return [df["do_predict"] == 1, df["up"] > PROB_THRESHOLD]
        return [df["do_predict"] == 1, df["&s-up_or_down"] == "up"]

    def _freqai_short_signal(self, df: DataFrame) -> list:
        if "up" in df.columns and "down" in df.columns:
            return [df["do_predict"] == 1, df["down"] > PROB_THRESHOLD]
        return [df["do_predict"] == 1, df["&s-up_or_down"] == "down"]

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        enter_long_conditions = [
            *self._freqai_long_signal(df),
            df["adx"] > ADX_MIN,
            df["close"] > df["ema50"],
            df["rsi"] > 35,
            df["rsi"] < 72,
            df["volume"] > 0,
        ]
        enter_short_conditions = [
            *self._freqai_short_signal(df),
            df["adx"] > ADX_MIN,
            df["close"] < df["ema50"],
            df["rsi"] > 28,
            df["rsi"] < 65,
            df["volume"] > 0,
        ]

        if enter_long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_long_conditions), ["enter_long", "enter_tag"]
            ] = (1, "freqai_hybrid_up")

        if enter_short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_short_conditions), ["enter_short", "enter_tag"]
            ] = (1, "freqai_hybrid_down")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        if "up" in df.columns and "down" in df.columns:
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
