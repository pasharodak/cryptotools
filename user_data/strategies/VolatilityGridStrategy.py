# pragma pylint: disable=missing-docstring, invalid-name
"""Volatility Grid — Bollinger Bands + ADX, grid DCA и частичный TP по середине BB."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pandas import DataFrame

import talib.abstract as ta
from ctengine.persistence import Trade
from ctengine.strategy import IStrategy
from technical import qtpylib

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
from ml.gate import allow_trade_entry, persist_entry_ml  # noqa: E402
from _sim_live import PROD_MINIMAL_ROI, PROD_STOPLOSS  # noqa: E402

GRID_SCENARIO = {
    "scenario_id": "live_grid",
    "scan_type": "grid",
    "group": "live",
    "strategy": "VolatilityGridStrategy",
    "label": "Grid (live)",
}


class VolatilityGridStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    position_adjustment_enable = False
    use_exit_signal = False
    trailing_stop = False

    adx_max = 22
    bb_width_min = 0.022
    pair_cooldown_minutes = 240

    stoploss = PROD_STOPLOSS
    minimal_roi = PROD_MINIMAL_ROI

    def _pair_in_cooldown(self, pair: str, current_time: datetime) -> bool:
        """Block re-entry after stoploss / emergency exit on the same pair."""
        closed = Trade.get_trades_proxy(pair=pair, is_open=False)
        if not closed:
            return False
        last = max(
            closed,
            key=lambda t: t.close_date or datetime.min.replace(tzinfo=UTC),
        )
        if not last.close_date:
            return False
        reason = (last.exit_reason or "").lower()
        if "stoploss" not in reason and reason != "emergency_exit":
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
        if self._pair_in_cooldown(pair, current_time):
            return False
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stake = float(self.config.get("stake_amount") or 0)
        return allow_trade_entry(
            scenario=GRID_SCENARIO,
            pair=pair,
            rate=rate,
            side=side,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=float(self.stoploss),
            minimal_roi=dict(self.minimal_roi),
            timeframe=self.timeframe,
            ohlcv_df=df,
        )

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
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stake = float(self.config.get("stake_amount") or 0)
        persist_entry_ml(
            trade,
            scenario=GRID_SCENARIO,
            ohlcv_df=df,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=float(self.stoploss),
            minimal_roi=dict(self.minimal_roi),
            timeframe=self.timeframe,
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bollinger = qtpylib.bollinger_bands(dataframe["close"], window=20, stds=2)
        dataframe["bb_lower"] = bollinger["lower"]
        dataframe["bb_mid"] = bollinger["mid"]
        dataframe["bb_upper"] = bollinger["upper"]
        dataframe["bb_width"] = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_mid"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def _ranging_volatile(self, dataframe: DataFrame) -> DataFrame:
        return (
            (dataframe["adx"] < self.adx_max)
            & (dataframe["bb_width"] >= self.bb_width_min)
            & (dataframe["volume"] > 0)
        )

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ranging = self._ranging_volatile(dataframe)

        dataframe.loc[
            ranging
            & (dataframe["close"] <= dataframe["bb_lower"])
            & (dataframe["rsi"] < 42),
            "enter_long",
        ] = 1

        dataframe.loc[
            ranging
            & (dataframe["close"] >= dataframe["bb_upper"])
            & (dataframe["rsi"] > 58),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
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
        return min(3.0, max_leverage)
