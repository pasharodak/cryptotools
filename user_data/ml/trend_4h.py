#!/usr/bin/env python3
"""4H trend model: build direction + strength from OHLCV (resampled to 4h).

Components (each in [-1, +1], then blended):
  - EMA stack (20/50/200 alignment + price vs EMA50)
  - ADX regime * DI sign
  - Swing structure (HH/HL vs LH/LL over lookback)
  - Momentum (ROC of close)

Output per bar:
  trend_dir   ∈ {-1, 0, +1}   (down / range / up)
  trend_score ∈ [-1, +1]      (signed strength)
  trend_strength ∈ [0, 1]     (|score|)
  regime      ∈ {trend_up, trend_down, range}
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

TREND_4H_FEATURES = [
    "trend_4h_dir",
    "trend_4h_score",
    "trend_4h_strength",
    "trend_4h_adx",
    "trend_4h_ema_align",
]


def resample_ohlcv(df: pd.DataFrame, rule: str = "4h") -> pd.DataFrame:
    """Resample OHLCV with DatetimeIndex (or `date` column) to `rule` bars."""
    if df.empty:
        return df.copy()
    if "date" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        frame = df.set_index(pd.to_datetime(df["date"], utc=True))
    else:
        frame = df.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("OHLCV needs DatetimeIndex or date column")
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in frame.columns:
        agg["volume"] = "sum"
    out = frame.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["close"])


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _adx_di(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr = _atr(high, low, close, period)
    plus_di = (
        100
        * pd.Series(plus_dm, index=close.index)
        .ewm(alpha=1 / period, min_periods=period, adjust=False)
        .mean()
        / atr.replace(0, np.nan)
    )
    minus_di = (
        100
        * pd.Series(minus_dm, index=close.index)
        .ewm(alpha=1 / period, min_periods=period, adjust=False)
        .mean()
        / atr.replace(0, np.nan)
    )
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return adx, plus_di, minus_di


def _swing_structure(high: pd.Series, low: pd.Series, lookback: int = 6) -> pd.Series:
    """+1 HH+HL, -1 LH+LL, else 0 — rolling over last `lookback` swings (bars)."""
    hh = high > high.shift(1)
    hl = low > low.shift(1)
    lh = high < high.shift(1)
    ll = low < low.shift(1)
    bull = (hh & hl).astype(float)
    bear = (lh & ll).astype(float)
    score = bull.rolling(lookback, min_periods=2).mean() - bear.rolling(lookback, min_periods=2).mean()
    return score.clip(-1, 1)


@dataclass
class Trend4hSnapshot:
    pair: str
    asof: str
    close: float
    trend_dir: int
    trend_score: float
    trend_strength: float
    regime: str
    adx: float
    ema_align: float
    structure: float
    momentum: float
    di_plus: float
    di_minus: float


class Trend4hModel:
    """Builds a continuous 4H trend score and discrete regime."""

    def __init__(
        self,
        *,
        adx_trend: float = 22.0,
        adx_strong: float = 30.0,
        range_score: float = 0.18,
        w_ema: float = 0.35,
        w_adx: float = 0.30,
        w_structure: float = 0.20,
        w_mom: float = 0.15,
    ):
        self.adx_trend = adx_trend
        self.adx_strong = adx_strong
        self.range_score = range_score
        self.w_ema = w_ema
        self.w_adx = w_adx
        self.w_structure = w_structure
        self.w_mom = w_mom

    def build_4h_frame(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """From any TF OHLCV → 4h bars with trend columns."""
        h4 = resample_ohlcv(ohlcv, "4h")
        if len(h4) < 60:
            return h4.assign(
                trend_4h_dir=0,
                trend_4h_score=np.nan,
                trend_4h_strength=np.nan,
                trend_4h_adx=np.nan,
                trend_4h_ema_align=np.nan,
                trend_4h_structure=np.nan,
                trend_4h_momentum=np.nan,
                trend_4h_regime="range",
            )

        c = h4["close"].astype(float)
        h = h4["high"].astype(float)
        l = h4["low"].astype(float)

        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()
        ema200 = c.ewm(span=200, adjust=False).mean()

        # EMA alignment: each condition contributes ±1/3
        align = (
            np.sign(ema20 - ema50)
            + np.sign(ema50 - ema200)
            + np.sign(c - ema50)
        ) / 3.0

        adx, plus_di, minus_di = _adx_di(h, l, c, 14)
        di_sign = np.sign(plus_di - minus_di)
        adx_norm = ((adx - self.adx_trend) / max(self.adx_strong - self.adx_trend, 1e-6)).clip(0, 1)
        adx_comp = di_sign * adx_norm

        structure = _swing_structure(h, l, lookback=6)

        # ~3 days of 4h momentum (18 bars) normalized softly via tanh
        roc = c.pct_change(18)
        mom = np.tanh(roc / 0.08)

        score = (
            self.w_ema * align
            + self.w_adx * adx_comp
            + self.w_structure * structure
            + self.w_mom * mom
        ).clip(-1, 1)

        strength = score.abs()
        direction = pd.Series(0, index=h4.index, dtype=int)
        direction = direction.mask(score >= self.range_score, 1)
        direction = direction.mask(score <= -self.range_score, -1)

        regime = pd.Series("range", index=h4.index, dtype=object)
        regime = regime.mask((direction == 1) & (adx >= self.adx_trend), "trend_up")
        regime = regime.mask((direction == -1) & (adx >= self.adx_trend), "trend_down")
        # Weak direction without ADX stays range even if score tips
        regime = regime.mask(adx < self.adx_trend, "range")
        direction = direction.mask(adx < self.adx_trend, 0)

        out = h4.copy()
        out["ema20"] = ema20
        out["ema50"] = ema50
        out["ema200"] = ema200
        out["di_plus"] = plus_di
        out["di_minus"] = minus_di
        out["trend_4h_adx"] = adx
        out["trend_4h_ema_align"] = align
        out["trend_4h_structure"] = structure
        out["trend_4h_momentum"] = mom
        out["trend_4h_score"] = score
        out["trend_4h_strength"] = strength
        out["trend_4h_dir"] = direction.astype(int)
        out["trend_4h_regime"] = regime
        return out

    def attach_to_base(
        self, base_ohlcv: pd.DataFrame, *, base_timeframe: str = "5m"
    ) -> pd.DataFrame:
        """Compute 4h trend and forward-fill onto base TF index."""
        del base_timeframe  # reserved for future bar-align rules
        h4 = self.build_4h_frame(base_ohlcv)
        cols = [
            "trend_4h_dir",
            "trend_4h_score",
            "trend_4h_strength",
            "trend_4h_adx",
            "trend_4h_ema_align",
            "trend_4h_structure",
            "trend_4h_momentum",
            "trend_4h_regime",
        ]
        if base_ohlcv.empty or h4.empty:
            out = base_ohlcv.copy()
            for c in cols:
                out[c] = np.nan if c != "trend_4h_regime" else "range"
            if "trend_4h_dir" in out.columns:
                out["trend_4h_dir"] = 0
            return out

        aligned = h4[cols].reindex(base_ohlcv.index, method="ffill")
        out = base_ohlcv.copy()
        for c in cols:
            out[c] = aligned[c]
        return out

    def snapshot(self, ohlcv: pd.DataFrame, *, pair: str = "") -> Trend4hSnapshot | None:
        h4 = self.build_4h_frame(ohlcv)
        if h4.empty or h4["trend_4h_score"].isna().all():
            return None
        row = h4.dropna(subset=["trend_4h_score"]).iloc[-1]
        return Trend4hSnapshot(
            pair=pair,
            asof=str(row.name),
            close=float(row["close"]),
            trend_dir=int(row["trend_4h_dir"]),
            trend_score=round(float(row["trend_4h_score"]), 4),
            trend_strength=round(float(row["trend_4h_strength"]), 4),
            regime=str(row["trend_4h_regime"]),
            adx=round(float(row["trend_4h_adx"]), 2),
            ema_align=round(float(row["trend_4h_ema_align"]), 4),
            structure=round(float(row["trend_4h_structure"]), 4),
            momentum=round(float(row["trend_4h_momentum"]), 4),
            di_plus=round(float(row["di_plus"]), 2),
            di_minus=round(float(row["di_minus"]), 2),
        )

    def snapshot_dict(self, ohlcv: pd.DataFrame, *, pair: str = "") -> dict[str, Any] | None:
        snap = self.snapshot(ohlcv, pair=pair)
        return asdict(snap) if snap else None
