"""Causal time-series entry features: ETS/ARIMA-style, lags, volume, GARCH-family vol."""
from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

# Extra features on top of core+wide market indicators.
TS_RICH_FEATURES = [
    # Exponential smoothing / Holt-ish
    "ets_level_dev",
    "ets_trend_12_48",
    "ets_forecast_ret",
    "ets_abs_resid",
    # AR / short memory
    "ar1_rho_96",
    "ar1_resid",
    "ar1_forecast_ret",
    # Lags of returns & volume
    "ret_lag_1",
    "ret_lag_2",
    "ret_lag_3",
    "ret_lag_5",
    "ret_lag_8",
    "vol_lag_1_ratio",
    "vol_lag_3_ratio",
    "vol_z_20",
    "vol_z_48",
    "vol_shock_5",
    "dollar_vol_z_20",
    # GARCH-family (RiskMetrics EWMA + optional ARCH recursion)
    "garch_ewma_vol",
    "garch_ewma_vol_z",
    "garch_arch_vol",
    "garch_arch_vol_z",
]


def _ewma_var(rets: pd.Series, lam: float = 0.94) -> pd.Series:
    """RiskMetrics / IGARCH-style recursive variance (causal)."""
    r2 = (rets.fillna(0.0) ** 2).to_numpy(dtype=float)
    out = np.empty_like(r2)
    v = float(np.nanmean(r2[:50])) if len(r2) >= 50 else float(np.nanmean(r2) or 1e-8)
    if not np.isfinite(v) or v <= 0:
        v = 1e-8
    for i, x in enumerate(r2):
        v = lam * v + (1.0 - lam) * x
        out[i] = v
    return pd.Series(out, index=rets.index)


def _rolling_ar1(rets: pd.Series, window: int = 96) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Fast AR(1): rho from rolling cov/var (vectorized), residual + one-step forecast."""
    r = rets.astype(float)
    x = r.shift(1)
    # Rolling cov(r_t, r_{t-1}) / var(r_{t-1})
    mean_r = r.rolling(window, min_periods=max(20, window // 3)).mean()
    mean_x = x.rolling(window, min_periods=max(20, window // 3)).mean()
    cov = ((r - mean_r) * (x - mean_x)).rolling(window, min_periods=max(20, window // 3)).mean()
    var_x = ((x - mean_x) ** 2).rolling(window, min_periods=max(20, window // 3)).mean()
    rho = (cov / var_x.replace(0, np.nan)).clip(-0.99, 0.99)
    fcast = rho * r  # one-step forecast of next return using current r
    resid = r - rho * x
    return rho, resid, fcast


def _arch_garch_vol(rets: pd.Series, warmup: int = 500) -> pd.Series:
    """Fit GARCH(1,1) once on warmup, then recurse variance (params frozen).

    Set CT_TS_USE_ARCH=0 to skip (fills NaN; EWMA vol still available).
    """
    import os

    out = pd.Series(np.nan, index=rets.index, dtype=float)
    if os.environ.get("CT_TS_USE_ARCH", "1").strip() in ("0", "false", "no"):
        return out
    r = rets.replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < warmup + 50:
        return out
    try:
        from arch import arch_model
    except Exception:
        return out

    # arch prefers percent returns scale
    train = (r.iloc[:warmup] * 100.0).astype(float)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            am = arch_model(train, mean="Zero", vol="GARCH", p=1, q=1, rescale=False)
            res = am.fit(disp="off", show_warning=False)
        params = res.params
        omega = float(params.get("omega", 1e-6))
        alpha = float(params.get("alpha[1]", 0.05))
        beta = float(params.get("beta[1]", 0.9))
    except Exception:
        return out

    # Recurse on full series in percent^2, then convert to decimal vol
    all_r = rets.fillna(0.0).to_numpy(dtype=float) * 100.0
    var = float(np.nanvar(all_r[:warmup]) or 1.0)
    vols = np.empty(len(all_r), dtype=float)
    for i, rt in enumerate(all_r):
        var = omega + alpha * (rt**2) + beta * var
        var = max(var, 1e-8)
        vols[i] = np.sqrt(var) / 100.0
    return pd.Series(vols, index=rets.index)


def compute_ts_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Build causal TS/lag/volume/vol features aligned to OHLCV index."""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=df.index)

    ret = c.pct_change(1)
    ewma12 = c.ewm(span=12, adjust=False).mean()
    ewma48 = c.ewm(span=48, adjust=False).mean()
    # Holt-ish: level + trend proxy; one-step forecast return ≈ trend/close
    ets_trend = ewma12 - ewma48
    ets_forecast_ret = ets_trend / c.replace(0, np.nan)
    ets_level_dev = (c - ewma12) / c.replace(0, np.nan)
    ets_resid = ret - ets_forecast_ret.shift(1)

    rho, ar_resid, ar_fcast = _rolling_ar1(ret, window=96)

    vol_sma20 = v.rolling(20, min_periods=5).mean()
    vol_sma48 = v.rolling(48, min_periods=10).mean()
    vol_std20 = v.rolling(20, min_periods=5).std()
    dollar = (v * c).replace(0, np.nan)
    dollar_z = (dollar - dollar.rolling(20, min_periods=5).mean()) / dollar.rolling(
        20, min_periods=5
    ).std().replace(0, np.nan)

    ewma_var = _ewma_var(ret, lam=0.94)
    ewma_vol = np.sqrt(ewma_var)
    ewma_vol_z = ewma_vol / ewma_vol.rolling(96, min_periods=20).mean().replace(0, np.nan)

    arch_vol = _arch_garch_vol(ret, warmup=min(500, max(200, len(ret) // 5)))
    arch_vol_z = arch_vol / arch_vol.rolling(96, min_periods=20).mean().replace(0, np.nan)

    out = pd.DataFrame(
        {
            "ets_level_dev": ets_level_dev,
            "ets_trend_12_48": ets_trend / c.replace(0, np.nan),
            "ets_forecast_ret": ets_forecast_ret,
            "ets_abs_resid": ets_resid.abs(),
            "ar1_rho_96": rho,
            "ar1_resid": ar_resid,
            "ar1_forecast_ret": ar_fcast,
            "ret_lag_1": ret.shift(1),
            "ret_lag_2": ret.shift(2),
            "ret_lag_3": ret.shift(3),
            "ret_lag_5": ret.shift(5),
            "ret_lag_8": ret.shift(8),
            "vol_lag_1_ratio": v / v.shift(1).replace(0, np.nan),
            "vol_lag_3_ratio": v / v.shift(3).replace(0, np.nan),
            "vol_z_20": (v - vol_sma20) / vol_std20.replace(0, np.nan),
            "vol_z_48": (v - vol_sma48) / v.rolling(48, min_periods=10).std().replace(0, np.nan),
            "vol_shock_5": v / v.rolling(5, min_periods=3).mean().replace(0, np.nan),
            "dollar_vol_z_20": dollar_z,
            "garch_ewma_vol": ewma_vol,
            "garch_ewma_vol_z": ewma_vol_z,
            "garch_arch_vol": arch_vol,
            "garch_arch_vol_z": arch_vol_z,
            # raw for sequence models
            "seq_ret": ret,
            "seq_range": (h - l) / c.replace(0, np.nan),
            "seq_vol_z": (v - vol_sma20) / vol_std20.replace(0, np.nan),
            "seq_body": (c - df["open"].astype(float)) / c.replace(0, np.nan)
            if "open" in df.columns
            else ret,
        },
        index=df.index,
    )
    return out.replace([np.inf, -np.inf], np.nan)


SEQ_FEATURE_COLS = ["seq_ret", "seq_range", "seq_vol_z", "seq_body"]


def sequences_at_times(
    ts_frame: pd.DataFrame,
    open_ms_list: list[int],
    *,
    window: int = 32,
) -> np.ndarray:
    """Build [N, T, F] sequences ending at each open_ms (pad with zeros if short)."""
    if ts_frame.empty:
        return np.zeros((len(open_ms_list), window, len(SEQ_FEATURE_COLS)), dtype=np.float32)
    mat = ts_frame[SEQ_FEATURE_COLS].to_numpy(dtype=np.float64)
    idx = ts_frame.index
    # Ensure UTC timestamps
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    out = np.zeros((len(open_ms_list), window, len(SEQ_FEATURE_COLS)), dtype=np.float32)
    for i, oms in enumerate(open_ms_list):
        ts = pd.Timestamp(int(oms), unit="ms", tz="UTC")
        pos = idx.get_indexer([ts], method="pad")[0]
        if pos < 0:
            continue
        start = max(0, pos - window + 1)
        chunk = mat[start : pos + 1]
        if len(chunk) == 0:
            continue
        # nan -> 0
        chunk = np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0)
        out[i, window - len(chunk) :, :] = chunk.astype(np.float32)
    return out
