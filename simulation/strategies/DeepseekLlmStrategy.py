"""Live LLM bot: DeepSeek via ShareLLM on liquid USDT perps.

Same cycle as Bitcoin-Deepseek-Trader (enrich candles → JSON signal → act
when confidence >= the configured minimum, default 0.65). Calls the API only for the newest candle, so a
historical backtest does not spend tokens. SL/TP executed by the router are
the fixed TAG_RISK levels (SL −2%, TP 3%), inside the example's 1–2% / 2–4% band.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from simulation.strategies.LiteFinanceStrategies import _LiteBase
from simulation.strategies.deepseek_openrouter import (
    LlmQuotaError,
    build_messages,
    candle_is_live,
    chat_completion,
    entry_sides,
    min_confidence,
    model_name,
    parse_prediction,
)

logger = logging.getLogger(__name__)

_DEFAULT_PAIRS = (
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT,"
    "XRP/USDT:USDT,DOGE/USDT:USDT,ADA/USDT:USDT,AVAX/USDT:USDT,"
    "LINK/USDT:USDT,LTC/USDT:USDT,DOT/USDT:USDT,TON/USDT:USDT"
)
_cache_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
_logged_config = False


def allowed_pairs() -> set[str]:
    raw = os.environ.get("CT_LLM_PAIRS")
    if raw is None or not str(raw).strip():
        raw = _DEFAULT_PAIRS
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def last_llm_confidence(pair: str) -> float:
    """Confidence of the latest cached decision for this pair, else 0."""
    with _cache_lock:
        row = _cache.get(pair) or {}
    try:
        return float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _num(value: Any, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def _candle_rows(frame: DataFrame, limit: int) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    tail = frame.tail(limit)
    for _, row in tail.iterrows():
        stamp = row.get("date")
        rows.append(
            {
                "t": str(stamp) if stamp is not None else "",
                "o": _num(row.get("open"), 6),
                "h": _num(row.get("high"), 6),
                "l": _num(row.get("low"), 6),
                "c": _num(row.get("close"), 6),
                "v": _num(row.get("volume"), 4),
            }
        )
    return rows


def _resample_1h(dataframe: DataFrame) -> DataFrame:
    if dataframe is None or dataframe.empty or "date" not in dataframe.columns:
        return DataFrame()
    work = dataframe.copy()
    work["date"] = work["date"]
    indexed = work.set_index("date").sort_index()
    hourly = (
        indexed.resample("1h")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["close"])
    )
    hourly = hourly.reset_index()
    return hourly


def _indicator_block(frame: DataFrame) -> dict[str, Any]:
    if frame is None or len(frame) < 30:
        return {}
    close = frame["close"]
    rsi = ta.RSI(frame, timeperiod=14)
    atr = ta.ATR(frame, timeperiod=14)
    adx = ta.ADX(frame, timeperiod=14)
    willr = ta.WILLR(frame, timeperiod=14)
    ema50 = ta.EMA(frame, timeperiod=50) if len(frame) >= 50 else None
    ema200 = ta.EMA(frame, timeperiod=200) if len(frame) >= 200 else None
    macd = ta.MACD(frame, fastperiod=12, slowperiod=26, signalperiod=9)
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    width = ((bb_mid + 2 * bb_std) - (bb_mid - 2 * bb_std)) / bb_mid.replace(0, 1)
    log_ret = close.pct_change()
    hist_vol = float(log_ret.tail(20).std() or 0.0)
    last = frame.iloc[-1]
    ema_cross = "NEUTRAL"
    if ema50 is not None and ema200 is not None:
        fast = float(ema50.iloc[-1])
        slow = float(ema200.iloc[-1])
        if fast == fast and slow == slow:
            ema_cross = "BULL" if fast > slow else "BEAR"
    macd_block = {}
    if macd is not None and len(macd):
        macd_block = {
            "macd": _num(macd["macd"].iloc[-1], 6),
            "signal": _num(macd["macdsignal"].iloc[-1], 6),
            "histogram": _num(macd["macdhist"].iloc[-1], 6),
        }
    return {
        "rsi": _num(rsi.iloc[-1], 2),
        "williams_r": _num(willr.iloc[-1], 2),
        "atr": _num(atr.iloc[-1], 6),
        "adx": _num(adx.iloc[-1], 2),
        "bollinger_width": _num(width.iloc[-1], 4),
        "historical_volatility": _num(hist_vol, 4),
        "ema_50_200": ema_cross,
        "macd": macd_block,
        "close": _num(last.get("close"), 6),
    }


def market_snapshot(dataframe: DataFrame) -> dict[str, Any]:
    hourly = _resample_1h(dataframe)
    last_close = _num(dataframe["close"].iloc[-1], 6) if len(dataframe) else None
    return {
        "price": last_close,
        "candles_5m": _candle_rows(dataframe, 10),
        "candles_1h": _candle_rows(hourly, 5),
        "indicators_5m": _indicator_block(dataframe),
        "indicators_1h": _indicator_block(hourly),
    }


def _symbol(pair: str) -> str:
    text = (pair or "BTC/USDT:USDT").upper()
    base = text.split("/")[0] if "/" in text else text.replace("USDT", "")
    return f"{base}USDT"


class DeepseekLlmStrategy(_LiteBase):
    """DeepSeek web-chat signal (OpenSeek) on the latest candle. No LightGBM gate."""

    stoploss = -0.02
    minimal_roi = {"0": 0.03, "60": 0.02, "180": 0}
    pair_cooldown_minutes = 30
    startup_candle_count = 420
    can_short = True
    use_exit_signal = False
    builtin_llm_gate = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        pair = str((metadata or {}).get("pair") or "")
        with _cache_lock:
            cached = dict(_cache.get(pair) or {})
        dataframe["llm_conf"] = float(cached.get("confidence") or 0.0)
        dataframe["llm_action"] = str(cached.get("action") or "")
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        if dataframe is None or dataframe.empty:
            return dataframe
        pair = str((metadata or {}).get("pair") or "")
        if pair not in allowed_pairs():
            return dataframe
        if os.environ.get("CT_LLM_DISABLE", "").strip() in {"1", "true", "TRUE", "yes"}:
            return dataframe
        last_ts = dataframe["date"].iloc[-1] if "date" in dataframe.columns else None
        if not candle_is_live(last_ts):
            return dataframe
        candle_key = str(last_ts)
        with _cache_lock:
            cached = _cache.get(pair)
            if cached and cached.get("candle") == candle_key:
                decision = cached
            else:
                decision = None
        if decision is None:
            decision = self._ask_model(pair, dataframe, candle_key)
        long_on, short_on = entry_sides(decision, min_conf=min_confidence())
        idx = dataframe.index[-1]
        if long_on:
            dataframe.loc[idx, "enter_long"] = 1
        elif short_on:
            dataframe.loc[idx, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def _ask_model(self, pair: str, dataframe: DataFrame, candle_key: str) -> dict[str, Any]:
        global _logged_config
        if not _logged_config:
            logger.info(
                "DeepSeek LLM: model=%s min_conf=%.2f pairs=%s",
                model_name(),
                min_confidence(),
                ",".join(sorted(allowed_pairs())),
            )
            _logged_config = True
        snapshot = market_snapshot(dataframe)
        messages = build_messages(_symbol(pair), snapshot)
        try:
            content = chat_completion(messages)
            decision = parse_prediction(content)
            decision["raw"] = content
        except LlmQuotaError:
            raise
        except Exception as exc:
            logger.warning("DeepSeek LLM call failed %s: %s", pair, exc)
            decision = {
                "action": "HOLD",
                "confidence": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "reasoning": "",
                "error": str(exc)[:200],
            }
        decision["candle"] = candle_key
        decision["pair"] = pair
        with _cache_lock:
            _cache[pair] = decision
        conf = float(decision.get("confidence") or 0.0)
        action = str(decision.get("action") or "HOLD")
        long_on, short_on = entry_sides(decision, min_conf=min_confidence())
        gate = "pass" if long_on or short_on else "skip"
        reasoning = " ".join(str(decision.get("reasoning") or "").split())
        raw = " ".join(str(decision.get("raw") or "").split())
        answer = reasoning or raw or str(decision.get("error") or "")
        logger.info(
            "DeepSeek %s %s conf=%.2f gate=%s sl=%s tp=%s | %s",
            pair,
            action,
            conf,
            gate,
            decision.get("stop_loss"),
            decision.get("take_profit"),
            answer,
        )
        return decision
