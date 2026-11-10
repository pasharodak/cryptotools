# pragma pylint: disable=missing-docstring, invalid-name
"""Grid bot — one order at the edge of a 1m/5m range, then the other edge.

Entry sits 7.5% inside the channel. Take profit is 65% of the width from
that edge. Stop is 20% of the width outside the channel. After price moves
1% in favor the stop locks a fee buffer past entry. A stop drops the
channel; a take profit waits for the opposite edge. The sideways call comes
from the 5m channel. How many channels are tracked is max_open_trades
(the «Боковиков» setting). Stake is stake_amount.
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from pandas import DataFrame

from ctengine.persistence import Trade
from ctengine.strategy import IStrategy, informative, stoploss_from_absolute

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
_STRATS = Path(__file__).resolve().parent
if str(_STRATS) not in sys.path:
    sys.path.insert(0, str(_STRATS))

from ml.gate import allow_trade_entry, persist_entry_ml  # noqa: E402
from range_edge import apply_active, mark_range, same_range, scratch_stop, side_plan  # noqa: E402

logger = logging.getLogger(__name__)

try:
    from _pair_guard import claim_entry  # noqa: E402
except Exception:  # noqa: BLE001
    def claim_entry(bot: str, pair: str, side: str) -> bool:  # type: ignore[misc]
        return False


GRID_SCENARIO = {
    "scenario_id": "live_grid",
    "scan_type": "grid",
    "group": "live",
    "strategy": "VolatilityGridStrategy",
    "label": "Grid (live)",
}

# Hard ceiling. The live stop is the channel stop, which is inside this.
# With 3x leverage, -0.20 stake is about 6.7% of price.
_HARD_STOP = -0.20
# Fallback only. custom_roi exits at the channel take profit, which is lower.
_HARD_ROI = {"0": 1.0}


class VolatilityGridStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "1m"
    process_only_new_candles = True
    startup_candle_count = 120

    position_adjustment_enable = False
    use_exit_signal = False
    use_custom_stoploss = True
    use_custom_roi = True
    trailing_stop = False

    # Search thresholds — same family as ranging_scan_config. Sim subclasses
    # still override adx_max / bb_width_min.
    adx_max = 26
    bb_width_min = 0.012
    bb_width_max = 0.065
    max_ema_slope = 0.004
    min_ranging_ratio = 0.55
    min_inside_bb_ratio = 0.65
    min_atr_ratio = 0.55
    max_atr_ratio = 1.55
    max_adx_slope = 2.0
    min_midline_crosses = 2
    min_range_stability = 0.42
    lookback_candles = 96

    stoploss = _HARD_STOP
    minimal_roi = _HARD_ROI

    order_types = {
        "entry": "limit",
        "exit": "market",
        "emergency_exit": "market",
        "force_exit": "market",
        "force_entry": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    unfilledtimeout = {"entry": 30, "exit": 10, "unit": "minutes"}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # pair -> frozen prices for the order that is about to fill
        self._pending: dict[str, dict[str, float]] = {}

    def _range_params(self) -> dict:
        return {
            "lookback": int(self.lookback_candles),
            "adx_max": float(self.adx_max),
            "bb_width_min": float(self.bb_width_min),
            "bb_width_max": float(self.bb_width_max),
            "max_ema_slope": float(self.max_ema_slope),
            "min_ranging_ratio": float(self.min_ranging_ratio),
            "min_inside_bb_ratio": float(self.min_inside_bb_ratio),
            "min_atr_ratio": float(self.min_atr_ratio),
            "max_atr_ratio": float(self.max_atr_ratio),
            "max_adx_slope": float(self.max_adx_slope),
            "min_midline_crosses": float(self.min_midline_crosses),
            "min_range_stability": float(self.min_range_stability),
        }

    @informative("5m")
    def populate_indicators_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return mark_range(dataframe, self._range_params())

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """One line per pass: how many channels exist and which sit at an edge."""
        if not getattr(self, "dp", None):
            return
        try:
            names = list(self.dp.current_whitelist())
        except Exception:
            return
        ranges = 0
        long_names: list[str] = []
        short_names: list[str] = []
        for name in names:
            try:
                frame, _ = self.dp.get_analyzed_dataframe(name, self.timeframe)
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            if int(last.get("active_ok") or 0) != 1:
                continue
            ranges += 1
            base = str(name).split("/")[0]
            if int(last.get("active_long_zone") or 0) == 1:
                long_names.append(base)
            elif int(last.get("active_short_zone") or 0) == 1:
                short_names.append(base)
        logger.info(
            "grid scan: пар %s, боковиков %s, у нижнего края %s, у верхнего края %s",
            len(names),
            ranges,
            ",".join(long_names) or "нет",
            ",".join(short_names) or "нет",
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        marked = mark_range(dataframe, self._range_params())
        return apply_active(marked, self.timeframe)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = str((metadata or {}).get("pair") or "")
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        try:
            from _pair_guard import pair_open_elsewhere

            if pair and pair_open_elsewhere("grid", pair):
                return dataframe
        except Exception:
            pass
        if "active_long_zone" not in dataframe.columns:
            return dataframe
        long_rows = dataframe["active_long_zone"].eq(1)
        short_rows = dataframe["active_short_zone"].eq(1)
        dataframe.loc[long_rows, "enter_long"] = 1
        dataframe.loc[long_rows, "enter_tag"] = "range_long"
        dataframe.loc[short_rows, "enter_short"] = 1
        dataframe.loc[short_rows, "enter_tag"] = "range_short"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def _last_row(self, pair: str) -> dict | None:
        if not getattr(self, "dp", None):
            return None
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return None
        row = frame.iloc[-1]
        if int(row.get("active_ok") or 0) != 1:
            return None
        low = float(row["active_low"])
        high = float(row["active_high"])
        if not (high > low > 0):
            return None
        return {
            "low": low,
            "high": high,
            "score": float(row.get("active_score") or 0),
            "entry_long": float(row["active_entry_long"]),
            "tp_long": float(row["active_tp_long"]),
            "sl_long": float(row["active_sl_long"]),
            "entry_short": float(row["active_entry_short"]),
            "tp_short": float(row["active_tp_short"]),
            "sl_short": float(row["active_sl_short"]),
        }

    def _tracked_limit(self) -> int:
        raw = (self.config or {}).get("max_open_trades")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return 0
        if n < 0:
            return 10_000
        return n

    def _in_tracked_set(self, pair: str) -> bool:
        """Keep at most N live channels — the bot setting «Боковиков»."""
        if not getattr(self, "dp", None):
            return True
        limit = self._tracked_limit()
        if limit <= 0:
            return False
        try:
            names = list(self.dp.current_whitelist())
        except Exception:
            return True
        scored: list[tuple[float, str]] = []
        for name in names:
            try:
                row = self._last_row(name)
            except Exception:
                continue
            if not row:
                continue
            if self._plan(name, row["low"], row["high"]) == "abandon":
                continue
            scored.append((row["score"], name))
        scored.sort(key=lambda item: item[0], reverse=True)
        allowed = {name for _, name in scored[:limit]}
        return pair in allowed

    def _plan(self, pair: str, low: float, high: float) -> str:
        try:
            closed = Trade.get_trades_proxy(pair=pair, is_open=False)
        except Exception:
            return "either"
        if not closed:
            return "either"
        last = max(closed, key=lambda t: t.close_date or datetime.min.replace(tzinfo=UTC))
        try:
            old_low = last.get_custom_data("range_low", None)
            old_high = last.get_custom_data("range_high", None)
        except Exception:
            old_low = old_high = None
        return side_plan(
            last.exit_reason,
            bool(getattr(last, "is_short", False)),
            same_range(old_low, old_high, low, high),
        )

    def _freeze(self, pair: str, side: str, row: dict) -> dict[str, float]:
        return {
            "range_low": row["low"],
            "range_high": row["high"],
            "entry_long": row["entry_long"],
            "tp_long": row["tp_long"],
            "sl_long": row["sl_long"],
            "entry_short": row["entry_short"],
            "tp_short": row["tp_short"],
            "sl_short": row["sl_short"],
            "side": 1.0 if side == "short" else 0.0,
        }

    def custom_entry_price(
        self,
        pair: str,
        trade: Trade | None,
        current_time: datetime,
        proposed_rate: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        row = self._last_row(pair)
        if not row:
            return proposed_rate
        frozen = self._freeze(pair, side, row)
        self._pending[pair] = frozen
        price = frozen["entry_short"] if side == "short" else frozen["entry_long"]
        if price > 0:
            return float(price)
        return proposed_rate

    def _levels(self, trade: Trade) -> dict[str, float] | None:
        try:
            low = trade.get_custom_data("range_low", None)
            high = trade.get_custom_data("range_high", None)
        except Exception:
            low = high = None
        if low and high and float(high) > float(low):
            return {
                "range_low": float(low),
                "range_high": float(high),
                "tp_long": float(trade.get_custom_data("tp_long", 0) or 0),
                "tp_short": float(trade.get_custom_data("tp_short", 0) or 0),
                "sl_long": float(trade.get_custom_data("sl_long", 0) or 0),
                "sl_short": float(trade.get_custom_data("sl_short", 0) or 0),
            }
        pending = self._pending.get(trade.pair)
        return pending

    def _remember(self, trade: Trade, levels: dict[str, float]) -> None:
        try:
            for key in ("range_low", "range_high", "tp_long", "tp_short", "sl_long", "sl_short"):
                if levels.get(key):
                    trade.set_custom_data(key, float(levels[key]))
        except Exception:
            pass

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        levels = self._levels(trade)
        if not levels:
            return None
        self._remember(trade, levels)
        stop = levels["sl_short"] if trade.is_short else levels["sl_long"]
        if not stop or stop <= 0 or current_rate <= 0:
            return None
        stop = scratch_stop(
            float(trade.open_rate or 0),
            float(current_rate),
            float(stop),
            bool(trade.is_short),
        )
        dist = stoploss_from_absolute(
            stop,
            current_rate,
            is_short=bool(trade.is_short),
            leverage=float(trade.leverage or 1.0),
        )
        if not dist or dist <= 0:
            return None
        return float(dist)

    def custom_roi(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        trade_duration: int,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | None:
        levels = self._levels(trade)
        if not levels:
            return None
        self._remember(trade, levels)
        tp = levels["tp_short"] if trade.is_short else levels["tp_long"]
        if not tp or tp <= 0:
            return None
        try:
            target = float(trade.calc_profit_ratio(tp))
        except Exception:
            return None
        if target <= 0:
            return None
        # Strict `>` in the engine: exit as price reaches the level.
        return target - 1e-8

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
        row = self._last_row(pair)
        if not row:
            logger.info("grid skip %s %s: нет боковика", pair, side)
            return False
        plan = self._plan(pair, row["low"], row["high"])
        if plan == "abandon":
            logger.info("grid skip %s %s: боковик оставлен после стопа", pair, side)
            return False
        if plan in ("long", "short") and plan != side:
            logger.info("grid skip %s %s: ждём сторону %s", pair, side, plan)
            return False
        if not self._in_tracked_set(pair):
            logger.info("grid skip %s %s: пара вне лимита боковиков", pair, side)
            return False

        frozen = self._freeze(pair, side, row)
        self._pending[pair] = frozen
        entry = frozen["entry_short"] if side == "short" else frozen["entry_long"]
        sl = frozen["sl_short"] if side == "short" else frozen["sl_long"]
        tp = frozen["tp_short"] if side == "short" else frozen["tp_long"]
        if entry <= 0:
            logger.info("grid skip %s %s: нет цены входа", pair, side)
            return False
        if side == "short":
            sl_ratio = (entry - sl) / entry
            tp_ratio = (entry - tp) / entry
        else:
            sl_ratio = (sl - entry) / entry
            tp_ratio = (tp - entry) / entry

        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stake = float(self.config.get("stake_amount") or 0)
        if not allow_trade_entry(
            scenario=GRID_SCENARIO,
            pair=pair,
            rate=rate,
            side=side,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=float(sl_ratio),
            minimal_roi={"0": float(max(tp_ratio, 0.0))},
            timeframe=self.timeframe,
            ohlcv_df=frame,
        ):
            logger.info("grid skip %s %s: ML gate", pair, side)
            return False
        if not claim_entry("grid", pair, side):
            logger.info("grid skip %s %s: пара занята другим ботом", pair, side)
            return False
        logger.info("grid entry %s %s @ %s", pair, side, entry)
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
        pending = self._pending.get(pair)
        if pending:
            self._remember(trade, pending)
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        stake = float(self.config.get("stake_amount") or 0)
        persist_entry_ml(
            trade,
            scenario=GRID_SCENARIO,
            ohlcv_df=frame,
            current_time=current_time,
            stake_usdt=stake,
            stoploss=float(self.stoploss),
            minimal_roi=dict(self.minimal_roi),
            timeframe=self.timeframe,
        )

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
