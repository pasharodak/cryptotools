# pragma pylint: disable=missing-docstring, invalid-name
"""Volatility Grid — Bollinger Bands + ADX, grid DCA и частичный TP по середине BB."""

from datetime import UTC, datetime, timedelta

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from technical import qtpylib


class VolatilityGridStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 50

    position_adjustment_enable = True
    max_entry_position_adjustment = 1  # 1 initial + 1 DCA max

    adx_max = 28
    bb_width_min = 0.018
    grid_step = 0.012
    partial_tp_profit = 0.004
    pair_cooldown_minutes = 120

    # ROI from config closes only the *remaining* slice after DCA/stop — can tag "roi"
    # while total trade is negative (leverage + realized stoploss). Exits: BB mid / partial TP.
    minimal_roi = {}

    def _stoploss_was_filled(self, trade: Trade) -> bool:
        return any(
            o.ft_order_side == "stoploss"
            and o.status == "closed"
            and (o.filled or 0) > 0
            for o in trade.orders
        )

    def _pair_in_cooldown(self, pair: str, current_time: datetime) -> bool:
        """Block re-entry after stoploss / emergency exit on the same pair."""
        last = (
            Trade.get_trades([Trade.pair == pair, Trade.is_open.is_(False)])
            .order_by(Trade.close_date.desc())
            .first()
        )
        if last is None or not last.close_date:
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
        return True

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
        trend_break = dataframe["adx"] > (self.adx_max + 7)

        dataframe.loc[
            (dataframe["close"] >= dataframe["bb_mid"]) | trend_break,
            "exit_long",
        ] = 1

        dataframe.loc[
            (dataframe["close"] <= dataframe["bb_mid"]) | trend_break,
            "exit_short",
        ] = 1

        return dataframe

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | None | tuple[float | None, str | None]:
        dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if dataframe.empty:
            return None

        last = dataframe.iloc[-1]
        filled_entries = trade.select_filled_orders(trade.entry_side)
        if not filled_entries:
            return None

        initial_stake = filled_entries[0].stake_amount
        exit_orders = [
            o for o in trade.orders if o.ft_order_side == trade.exit_side and o.status == "closed"
        ]
        partial_done = any(o.ft_order_tag == "partial_tp_bb_mid" for o in exit_orders)

        if (
            not partial_done
            and current_profit > self.partial_tp_profit
        ):
            at_mid = (
                last["close"] >= last["bb_mid"] * 0.998
                and last["close"] <= last["bb_mid"] * 1.002
            )
            if at_mid:
                partial_stake = trade.stake_amount * 0.5
                if min_stake and partial_stake < min_stake:
                    return None
                return (-partial_stake, "partial_tp_bb_mid")

        if self._stoploss_was_filled(trade):
            return None

        if trade.nr_of_successful_entries > self.max_entry_position_adjustment:
            return None

        adverse_move = -self.grid_step * trade.nr_of_successful_entries
        if current_profit > adverse_move:
            return None

        add_stake = min(initial_stake, max_stake)
        if min_stake and add_stake < min_stake:
            return None
        return (add_stake, "grid_dca")

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
