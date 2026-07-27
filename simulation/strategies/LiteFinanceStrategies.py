# LiteFinance strategies — simulation player (v4: top-5 pairs, stake 10, less churn).
"""Seven styles from LiteFinance article, tuned for 5m futures + fee-aware targets."""

from datetime import UTC, datetime, timedelta

import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame
from technical import qtpylib


class _LiteBase(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 210
    use_exit_signal = True
    pair_cooldown_minutes = 180

    def _bb(self, df: DataFrame) -> DataFrame:
        bb = qtpylib.bollinger_bands(df["close"], window=20, stds=2)
        df["bb_lower"] = bb["lower"]
        df["bb_mid"] = bb["mid"]
        df["bb_upper"] = bb["upper"]
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, 1)
        return df

    def _pair_in_cooldown(self, pair: str, current_time: datetime) -> bool:
        closed = Trade.get_trades_proxy(pair=pair, is_open=False)
        if not closed:
            return False
        last = max(closed, key=lambda t: t.close_date or datetime.min.replace(tzinfo=UTC))
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
        return not self._pair_in_cooldown(pair, current_time)


class LitePositionStrategy(_LiteBase):
    """HODL — EMA50/200 cross only in clear trend + volume."""

    stoploss = -0.08
    minimal_roi = {"0": 0.05, "2880": 0.025, "7200": 0.01, "14400": 0}
    pair_cooldown_minutes = 1440

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema50_slope"] = dataframe["ema50"] - dataframe["ema50"].shift(3)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = qtpylib.crossed_above(dataframe["ema50"], dataframe["ema200"])
        cross_dn = qtpylib.crossed_below(dataframe["ema50"], dataframe["ema200"])
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.9
        trend = dataframe["adx"].between(20, 35)
        sep = (dataframe["ema50"] - dataframe["ema200"]).abs() / dataframe["close"] > 0.005
        dataframe.loc[cross_up & vol & trend & sep & (dataframe["ema50_slope"] > 0), "enter_long"] = 1
        dataframe.loc[cross_dn & vol & trend & sep & (dataframe["ema50_slope"] < 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[qtpylib.crossed_below(dataframe["ema50"], dataframe["ema200"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["ema50"], dataframe["ema200"]), "exit_short"] = 1
        return dataframe


class LiteSwingStrategy(_LiteBase):
    """Swing — RSI pullback in trend + volume confirmation."""

    stoploss = -0.05
    minimal_roi = {"0": 0.025, "720": 0.015, "2880": 0.008, "5760": 0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 0.8
        up = dataframe["close"] > dataframe["ema50"]
        dn = dataframe["close"] < dataframe["ema50"]
        trend = dataframe["adx"].between(15, 40)
        dataframe.loc[up & vol & trend & (dataframe["rsi"] < 40), "enter_long"] = 1
        dataframe.loc[dn & vol & trend & (dataframe["rsi"] > 60), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 70, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 30, "exit_short"] = 1
        return dataframe


class LiteIntradayStrategy(_LiteBase):
    """Intraday — selective MACD; max ~few trades/day per pair."""

    stoploss = -0.022
    minimal_roi = {"0": 0.022, "480": 0.012, "960": 0}
    pair_cooldown_minutes = 360

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["hist_rise"] = (dataframe["macdhist"] > dataframe["macdhist"].shift(1)) & (
            dataframe["macdhist"].shift(1) > dataframe["macdhist"].shift(2)
        )
        dataframe["hist_fall"] = (dataframe["macdhist"] < dataframe["macdhist"].shift(1)) & (
            dataframe["macdhist"].shift(1) < dataframe["macdhist"].shift(2)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = dataframe["adx"].between(17, 21)
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.35
        cross_up = qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
        cross_dn = qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
        strong_hist = dataframe["macdhist"].abs() > dataframe["macdhist"].rolling(20).mean().abs()
        dataframe.loc[cross_up & regime & vol & dataframe["hist_rise"] & strong_hist, "enter_long"] = 1
        dataframe.loc[cross_dn & regime & vol & dataframe["hist_fall"] & strong_hist, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_dn = qtpylib.crossed_below(dataframe["macd"], dataframe["macdsignal"])
        cross_up = qtpylib.crossed_above(dataframe["macd"], dataframe["macdsignal"])
        dataframe.loc[cross_dn, "exit_long"] = 1
        dataframe.loc[cross_up, "exit_short"] = 1
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime, current_rate: float, current_profit: float, **kwargs):
        open_dt = trade.open_date_utc if hasattr(trade, "open_date_utc") else trade.open_date
        if open_dt and open_dt.date() < current_time.date():
            return "intraday_close"


class LiteRangeStrategy(_LiteBase):
    """Range — BB bounce with rejection; 5x leverage in sim profile."""

    stoploss = -0.018
    minimal_roi = {"0": 0.022, "40": 0.012, "100": 0}
    pair_cooldown_minutes = 360
    sim_leverage = 1.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._bb(dataframe)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ranging = (dataframe["adx"] < 18) & dataframe["bb_width"].between(0.015, 0.055)
        vol_spike = dataframe["volume"] > dataframe["vol_sma"] * 1.25
        reject_long = (dataframe["low"] <= dataframe["bb_lower"]) & (dataframe["close"] > dataframe["bb_lower"])
        reject_short = (dataframe["high"] >= dataframe["bb_upper"]) & (dataframe["close"] < dataframe["bb_upper"])
        dataframe.loc[ranging & vol_spike & reject_long & (dataframe["rsi"] < 38), "enter_long"] = 1
        dataframe.loc[ranging & vol_spike & reject_short & (dataframe["rsi"] > 62), "enter_short"] = 1
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


class LiteScalpingStrategy(_LiteBase):
    """Scalping — EMA cross with min volatility; ROI covers ~0.12% fees."""

    stoploss = -0.009
    minimal_roi = {"0": 0.007, "30": 0.004, "75": 0}
    pair_cooldown_minutes = 150

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe = self._bb(dataframe)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["ema_spread"] = (dataframe["ema9"] - dataframe["ema21"]).abs() / dataframe["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        active = (
            (dataframe["bb_width"] > 0.022)
            & (dataframe["adx"] < 22)
            & (dataframe["volume"] > dataframe["vol_sma"] * 1.15)
            & (dataframe["ema_spread"] > 0.0015)
        )
        up = qtpylib.crossed_above(dataframe["ema9"], dataframe["ema21"])
        dn = qtpylib.crossed_below(dataframe["ema9"], dataframe["ema21"])
        dataframe.loc[active & up, "enter_long"] = 1
        dataframe.loc[active & dn, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dn = qtpylib.crossed_below(dataframe["ema9"], dataframe["ema21"])
        up = qtpylib.crossed_above(dataframe["ema9"], dataframe["ema21"])
        dataframe.loc[dn, "exit_long"] = 1
        dataframe.loc[up, "exit_short"] = 1
        return dataframe


class LiteHftStrategy(_LiteBase):
    """HFT sim — impulse + volume; higher TP vs fees, fewer junk entries."""

    stoploss = -0.006
    minimal_roi = {"0": 0.0025, "8": 0.0012, "20": 0}
    pair_cooldown_minutes = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        dataframe["mom"] = dataframe["close"].pct_change(3)
        dataframe["vol_sma"] = dataframe["volume"].rolling(12).mean()
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hot = dataframe["volume"] > dataframe["vol_sma"] * 1.5
        calm = dataframe["adx"] < 32
        dataframe.loc[hot & calm & (dataframe["mom"] > 0.0025) & (dataframe["rsi"] < 68), "enter_long"] = 1
        dataframe.loc[hot & calm & (dataframe["mom"] < -0.0025) & (dataframe["rsi"] > 32), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 78, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 22, "exit_short"] = 1
        return dataframe


class LiteArbitrageStrategy(_LiteBase):
    """Mean-reversion — z-score extremes only in quiet flat regimes."""

    stoploss = -0.008
    minimal_roi = {"0": 0.005, "20": 0.0025, "60": 0}
    pair_cooldown_minutes = 180

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._bb(dataframe)
        dataframe["sma48"] = ta.SMA(dataframe, timeperiod=48)
        dataframe["std48"] = dataframe["close"].rolling(48).std()
        dataframe["zscore"] = (dataframe["close"] - dataframe["sma48"]) / dataframe["std48"].replace(0, 1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        flat = (dataframe["adx"] < 16) & (dataframe["bb_width"] < 0.045)
        quiet = dataframe["volume"] < dataframe["vol_sma"] * 1.1
        turn_long = qtpylib.crossed_above(dataframe["zscore"], -3.0)
        turn_short = qtpylib.crossed_below(dataframe["zscore"], 3.0)
        dataframe.loc[flat & quiet & turn_long, "enter_long"] = 1
        dataframe.loc[flat & quiet & turn_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["zscore"] > 0, "exit_long"] = 1
        dataframe.loc[dataframe["zscore"] < 0, "exit_short"] = 1
        return dataframe


class SimFreqaiProxyStrategy(_LiteBase):
    """FreqAI hybrid proxy — rare trend impulses only (no ML)."""

    stoploss = -0.04
    minimal_roi = {"0": 0.035, "120": 0.018, "360": 0.005, "720": 0}
    pair_cooldown_minutes = 4320
    startup_candle_count = 210

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["mom"] = dataframe["close"].pct_change(12)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol = dataframe["volume"] > dataframe["vol_sma"] * 1.5
        regime = dataframe["adx"].between(26, 32)
        aligned_up = (dataframe["ema50"] > dataframe["ema200"]) & (dataframe["close"] > dataframe["ema50"])
        aligned_dn = (dataframe["ema50"] < dataframe["ema200"]) & (dataframe["close"] < dataframe["ema50"])
        impulse_up = qtpylib.crossed_above(dataframe["mom"], 0.007)
        impulse_dn = qtpylib.crossed_below(dataframe["mom"], -0.007)
        dataframe.loc[vol & regime & aligned_up & impulse_up & dataframe["rsi"].between(40, 65), "enter_long"] = 1
        dataframe.loc[vol & regime & aligned_dn & impulse_dn & dataframe["rsi"].between(35, 60), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["rsi"] > 72, "exit_long"] = 1
        dataframe.loc[dataframe["rsi"] < 28, "exit_short"] = 1
        dataframe.loc[qtpylib.crossed_below(dataframe["close"], dataframe["ema50"]), "exit_long"] = 1
        dataframe.loc[qtpylib.crossed_above(dataframe["close"], dataframe["ema50"]), "exit_short"] = 1
        return dataframe
