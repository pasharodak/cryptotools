"""Entry-time market indicators from OHLCV (5m default)."""
from __future__ import annotations

import numpy as np
import pandas as pd

TREND_12H_BARS_BY_TF = {"5m": 144, "15m": 48, "1h": 12}
TREND_24H_BARS_BY_TF = {"5m": 288, "15m": 96, "1h": 24}
TREND_48H_BARS_BY_TF = {"5m": 576, "15m": 192, "1h": 48}


def trend_12h_bars(timeframe: str = "5m") -> int:
    return TREND_12H_BARS_BY_TF.get(timeframe, 144)


def trend_24h_bars(timeframe: str = "5m") -> int:
    return TREND_24H_BARS_BY_TF.get(timeframe, 288)


def trend_48h_bars(timeframe: str = "5m") -> int:
    return TREND_48H_BARS_BY_TF.get(timeframe, 576)


def min_indicator_warmup(timeframe: str = "5m") -> int:
    return max(30, trend_48h_bars(timeframe) + 1)


MARKET_FEATURES_CORE = [
    "rsi_14",
    "atr_pct_14",
    "adx_14",
    "vol_ratio_20",
    "ret_1",
    "ret_4",
    "ret_12",
    "trend_12h",
    "ema_spread_pct",
    "range_pct",
    "dist_high_20_pct",
    "dist_low_20_pct",
]

MARKET_FEATURES_WIDE = [
    "rsi_28",
    "atr_pct_28",
    "adx_28",
    "vol_ratio_48",
    "ret_24",
    "ret_48",
    "trend_24h",
    "trend_48h",
    "ema_spread_slow_pct",
    "dist_high_48_pct",
    "dist_low_48_pct",
    "bb_width_20",
    "trend_4h_dir",
    "trend_4h_score",
    "trend_4h_strength",
    "trend_4h_adx",
    "trend_4h_ema_align",
]

# Default = core (prod pack models). Wide models read extra cols from OHLCV dict.
MARKET_FEATURES = list(MARKET_FEATURES_CORE)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr = _atr(high, low, close, period)
    plus_di = 100 * pd.Series(plus_dm, index=close.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=close.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_indicator_frame(
    df: pd.DataFrame, *, timeframe: str = "5m", include_trend_4h: bool = True
) -> pd.DataFrame:
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    atr = _atr(h, l, c, 14)
    atr28 = _atr(h, l, c, 28)
    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    ema55 = c.ewm(span=55, adjust=False).mean()
    high_20 = h.rolling(20, min_periods=5).max()
    low_20 = l.rolling(20, min_periods=5).min()
    high_48 = h.rolling(48, min_periods=10).max()
    low_48 = l.rolling(48, min_periods=10).min()
    vol_sma = v.rolling(20, min_periods=5).mean()
    vol_sma_48 = v.rolling(48, min_periods=10).mean()
    mid_20 = c.rolling(20, min_periods=5).mean()
    std_20 = c.rolling(20, min_periods=5).std()

    out = pd.DataFrame(
        {
            "rsi_14": _rsi(c, 14),
            "atr_pct_14": atr / c.replace(0, np.nan),
            "adx_14": _adx(h, l, c, 14),
            "vol_ratio_20": v / vol_sma.replace(0, np.nan),
            "ret_1": c.pct_change(1),
            "ret_4": c.pct_change(4),
            "ret_12": c.pct_change(12),
            "trend_12h": c.pct_change(trend_12h_bars(timeframe)),
            "ema_spread_pct": (ema9 - ema21) / c.replace(0, np.nan),
            "range_pct": (h - l) / c.replace(0, np.nan),
            "dist_high_20_pct": (c - high_20) / c.replace(0, np.nan),
            "dist_low_20_pct": (c - low_20) / c.replace(0, np.nan),
            "rsi_28": _rsi(c, 28),
            "atr_pct_28": atr28 / c.replace(0, np.nan),
            "adx_28": _adx(h, l, c, 28),
            "vol_ratio_48": v / vol_sma_48.replace(0, np.nan),
            "ret_24": c.pct_change(24),
            "ret_48": c.pct_change(48),
            "trend_24h": c.pct_change(trend_24h_bars(timeframe)),
            "trend_48h": c.pct_change(trend_48h_bars(timeframe)),
            "ema_spread_slow_pct": (ema21 - ema55) / c.replace(0, np.nan),
            "dist_high_48_pct": (c - high_48) / c.replace(0, np.nan),
            "dist_low_48_pct": (c - low_48) / c.replace(0, np.nan),
            "bb_width_20": (2.0 * std_20) / mid_20.replace(0, np.nan),
        },
        index=df.index,
    )
    if include_trend_4h and timeframe in ("5m", "15m", "1m", "1h"):
        try:
            from ml.trend_4h import Trend4hModel
        except ImportError:
            try:
                from simulation.ml.trend_4h import Trend4hModel
            except ImportError:
                Trend4hModel = None  # type: ignore
        if Trend4hModel is not None:
            try:
                cols = ["open", "high", "low", "close"]
                if "volume" in df.columns:
                    cols.append("volume")
                base = df[cols].copy()
                if not isinstance(base.index, pd.DatetimeIndex):
                    base.index = out.index
                enriched = Trend4hModel().attach_to_base(base, base_timeframe=timeframe)
                for col in (
                    "trend_4h_dir",
                    "trend_4h_score",
                    "trend_4h_strength",
                    "trend_4h_adx",
                    "trend_4h_ema_align",
                ):
                    if col in enriched.columns:
                        out[col] = enriched[col].reindex(out.index)
            except Exception:
                pass
    return out


def features_from_ohlcv(df: pd.DataFrame) -> dict[str, float]:
    """Return core+wide indicators from the last bar (gate picks what the model needs)."""
    all_keys = list(MARKET_FEATURES_CORE) + list(MARKET_FEATURES_WIDE)
    empty = {k: float("nan") for k in all_keys}
    if df is None or df.empty:
        return empty
    cols = {"open", "high", "low", "close", "volume"}
    if not cols.issubset(df.columns):
        return empty
    ind = compute_indicator_frame(df[list(cols)])
    if ind.empty:
        return empty
    row = ind.iloc[-1]
    out: dict[str, float] = {}
    for col in all_keys:
        if col not in ind.columns:
            out[col] = float("nan")
            continue
        val = row.get(col)
        out[col] = float(val) if val is not None and not pd.isna(val) else float("nan")
    return out
