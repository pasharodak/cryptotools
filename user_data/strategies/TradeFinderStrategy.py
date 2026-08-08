# pragma pylint: disable=missing-docstring, invalid-name
"""ML Trade Finder — XGBoost scanner every scan_stride bars + pnl classifier gate."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from pandas import DataFrame

from ctengine.persistence import Trade
from ctengine.strategy import IStrategy

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
from ml.finder_live import FinderLive  # noqa: E402
from ml.gate import save_ml_to_trade  # noqa: E402
from _sim_live import PROD_MINIMAL_ROI, PROD_STOPLOSS  # noqa: E402


class TradeFinderStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 150
    use_exit_signal = False
    trailing_stop = False

    stoploss = PROD_STOPLOSS
    minimal_roi = PROD_MINIMAL_ROI

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.finder = FinderLive()
        self._cooldown_until: dict[str, int] = {}
        self._pending_by_pair: dict[str, dict] = {}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        pair = metadata["pair"]
        if len(dataframe) < 2:
            return dataframe

        bar_ms = int(dataframe["date"].iloc[-1].timestamp() * 1000)
        if self._cooldown_until.get(pair, 0) > bar_ms:
            return dataframe

        signal = self.finder.scan_last_bar(pair, dataframe)
        if not signal:
            return dataframe

        if signal["is_short"]:
            dataframe.loc[dataframe.index[-1], "enter_short"] = 1
            dataframe.loc[dataframe.index[-1], "enter_tag"] = "finder_short"
        else:
            dataframe.loc[dataframe.index[-1], "enter_long"] = 1
            dataframe.loc[dataframe.index[-1], "enter_tag"] = "finder_long"

        self._pending_by_pair[pair] = signal
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

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
        signal = self._pending_by_pair.get(pair)
        if not signal:
            signal = self.finder.scan_last_bar(pair, self.dp.get_analyzed_dataframe(pair, self.timeframe)[0])
        if not signal:
            return False
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if not self.finder.allow_with_classifier_gate(signal, current_time=current_time, ohlcv_df=df):
            return False
        self._pending_by_pair[pair] = signal
        return True

    def order_filled(
        self,
        pair: str,
        trade: Trade,
        order,
        current_time: datetime,
        **kwargs,
    ) -> None:
        if order.ft_order_side != trade.entry_side:
            return
        signal = self._pending_by_pair.get(pair)
        if not signal:
            return
        if signal.get("inverted"):
            trade.set_custom_data("finder_inverted", True)
            if signal.get("finder_model_side"):
                trade.set_custom_data("finder_model_side", str(signal["finder_model_side"]))
        save_ml_to_trade(
            trade,
            signal.get("finder_ml"),
            gate_ml=signal.get("gate_ml"),
        )
        self._cooldown_until[pair] = int(current_time.timestamp() * 1000) + int(signal["cooldown_ms"])
        self._pending_by_pair.pop(pair, None)

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
