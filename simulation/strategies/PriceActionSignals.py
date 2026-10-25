"""Pure signal logic for the Price Action bot.

Two setups share one 0-8 score:
- Trend Pullback: 1h/15m trend, pullback to VWAP / broken level, 5m + 1m trigger.
- Liquidity Sweep: prior swing is swept and reclaimed inside a range.

No exchange I/O and no config files: the runtime bot passes closed OHLCV
frames plus public market metrics. The score is a rule-based quality score,
not a probability.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

FEE_TAKER = 0.00055
FEE_ROUND_TRIP = FEE_TAKER * 2.0
FEE_USDT_PER_1000 = 1000.0 * FEE_ROUND_TRIP

TAGS = {
    "trend_pullback": "PaTrendPullback",
    "liquidity_sweep": "PaLiquiditySweep",
}
SETUP_LABELS = {
    "trend_pullback": "Trend Pullback",
    "liquidity_sweep": "Liquidity Sweep",
}

DEFAULT_MIN_SCORE = 6
DEFAULT_MIN_RR = 1.5
DEFAULT_MAX_SPREAD_PCT = 0.08
DEFAULT_MIN_DEPTH_USDT = 20_000.0
DEFAULT_MIN_TURNOVER_USDT = 20_000_000.0
DEFAULT_MAX_RISK_PCT = 2.5
DEFAULT_MIN_RISK_PCT = 0.10
DEFAULT_FUNDING_INTERVALS = 1.0

REGIME_LABELS = {
    "trend_up": "Тренд ↑",
    "trend_down": "Тренд ↓",
    "impulse_up": "Импульс ↑",
    "impulse_down": "Импульс ↓",
    "range": "Рендж",
    "risky": "Повышенный риск",
}


def _f(value: Any, default: float | None = 0.0) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(out):
        return default
    return out


def _last(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    return _f(series.iloc[-1], None)


def _prep(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    out = frame.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            return None
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "dt" not in out.columns:
        if "date" in out.columns:
            out["dt"] = pd.to_datetime(out["date"], utc=True, errors="coerce")
        elif "date_ms" in out.columns:
            out["dt"] = pd.to_datetime(
                pd.to_numeric(out["date_ms"], errors="coerce"), unit="ms", utc=True
            )
        else:
            out["dt"] = pd.to_datetime(out.index, unit="ms", utc=True, errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"]).copy()
    out = out.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
    out["volume"] = out["volume"].fillna(0.0)
    return out if len(out) >= 20 else None


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = _atr(df, period)
    plus_di = 100.0 * plus_dm.rolling(period, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.rolling(period, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(period, min_periods=period).mean()


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df["dt"].dt.floor("D")
    cum_value = (typical * df["volume"]).groupby(day).cumsum()
    cum_volume = df["volume"].groupby(day).cumsum()
    vwap = cum_value / cum_volume.replace(0, np.nan)
    rolling = (typical * df["volume"]).rolling(96, min_periods=20).sum() / df[
        "volume"
    ].rolling(96, min_periods=20).sum().replace(0, np.nan)
    return vwap.fillna(rolling)


def _swings(df: pd.DataFrame, *, left: int = 3, right: int = 3, lookback: int = 140):
    highs: list[float] = []
    lows: list[float] = []
    start = max(left, len(df) - lookback)
    for i in range(start, len(df) - right):
        window = df.iloc[i - left : i + right + 1]
        bar_high = float(df["high"].iloc[i])
        bar_low = float(df["low"].iloc[i])
        if bar_high >= float(window["high"].max()):
            highs.append(bar_high)
        if bar_low <= float(window["low"].min()):
            lows.append(bar_low)
    return highs, lows


def _touches(df: pd.DataFrame, level: float, *, lookback: int = 120, tol: float = 0.0008) -> int:
    window = df.iloc[-lookback:]
    if window.empty or level <= 0:
        return 0
    band = level * tol
    return int(((window["low"] <= level + band) & (window["high"] >= level - band)).sum())


def _one_minute_confirm(df1m: pd.DataFrame | None, side: str) -> bool | None:
    if df1m is None or len(df1m) < 12:
        return None
    close = df1m["close"]
    ema9 = _ema(close, 9)
    last = float(close.iloc[-1])
    prev3 = float(close.iloc[-4])
    last_bar = df1m.iloc[-1]
    bullish_bar = float(last_bar["close"]) > float(last_bar["open"])
    if side == "long":
        return bool(last > float(ema9.iloc[-1]) and last >= prev3 and bullish_bar)
    return bool(last < float(ema9.iloc[-1]) and last <= prev3 and not bullish_bar)


def _regime(ctx: dict[str, Any]) -> dict[str, Any]:
    close = ctx["close"]
    atr1h = ctx["atr_1h"] or 0.0
    adx1h = ctx["adx_1h"] or 0.0
    adx15 = ctx["adx_15m"] or 0.0
    ema20_1h = ctx["ema20_1h"] or close
    ema50_1h = ctx["ema50_1h"] or close
    ema20_15 = ctx["ema20_15m"] or close
    atr15 = ctx["atr_15m"] or 0.0
    atr_pct = (atr1h / close * 100.0) if close > 0 else 0.0
    up = ema20_1h > ema50_1h and close > ema50_1h
    down = ema20_1h < ema50_1h and close < ema50_1h
    move6 = 0.0
    df1h = ctx["df1h"]
    if len(df1h) >= 8 and atr1h > 0:
        move6 = close - float(df1h["close"].iloc[-7])
    key = "range"
    if atr_pct >= 2.5:
        key = "risky"
    elif atr1h > 0 and abs(move6) >= 3.0 * atr1h:
        key = "impulse_up" if move6 > 0 else "impulse_down"
    elif adx15 >= 25 and atr15 > 0 and abs(close - ema20_15) >= 1.5 * atr15:
        key = "impulse_up" if close > ema20_15 else "impulse_down"
    elif adx1h >= 22 and up:
        key = "trend_up"
    elif adx1h >= 22 and down:
        key = "trend_down"
    return {
        "key": key,
        "label": REGIME_LABELS[key],
        "adx_1h": round(float(adx1h), 1),
        "adx_15m": round(float(adx15), 1),
        "atr_pct_1h": round(float(atr_pct), 3),
        "trend_dir": 1 if up else -1 if down else 0,
        "range_high": ctx["range_high"],
        "range_low": ctx["range_low"],
        "range_width_pct": round(
            ((ctx["range_high"] - ctx["range_low"]) / close * 100.0) if close > 0 else 0.0, 3
        ),
    }


def _htf_score(ctx: dict[str, Any], side: str) -> int:
    close = ctx["close"]
    up1 = (ctx["ema20_1h"] or 0) > (ctx["ema50_1h"] or 0) and close > (ctx["ema50_1h"] or close)
    dn1 = (ctx["ema20_1h"] or 0) < (ctx["ema50_1h"] or 0) and close < (ctx["ema50_1h"] or close)
    close15 = ctx["close_15m"]
    up15 = (ctx["ema20_15m"] or 0) > (ctx["ema50_15m"] or 0) and close15 > (ctx["ema20_15m"] or close15)
    dn15 = (ctx["ema20_15m"] or 0) < (ctx["ema50_15m"] or 0) and close15 < (ctx["ema20_15m"] or close15)
    if side == "long":
        return 2 if up1 and up15 else 1 if up1 or up15 else 0
    return 2 if dn1 and dn15 else 1 if dn1 or dn15 else 0


def _sweep_htf_score(ctx: dict[str, Any], side: str) -> int:
    regime = ctx["regime"]["key"]
    if regime == "range":
        return 2
    if side == "long":
        if regime in ("trend_up", "impulse_up"):
            return 2
        if regime in ("trend_down", "impulse_down"):
            return 0
    else:
        if regime in ("trend_down", "impulse_down"):
            return 2
        if regime in ("trend_up", "impulse_up"):
            return 0
    return 1


def _flow_score(ctx: dict[str, Any], side: str) -> tuple[int, str]:
    bid = ctx["bid_depth_usdt"] or 0.0
    ask = ctx["ask_depth_usdt"] or 0.0
    total = bid + ask
    if total <= 0:
        return 0, "нет данных стакана"
    ratio = bid / ask if ask > 0 else 0.0
    df5 = ctx["df5"]
    signed = float(
        (
            (df5["close"].iloc[-3:] - df5["open"].iloc[-3:])
            * df5["volume"].iloc[-3:]
        ).sum()
    )
    if side == "long":
        ok = ratio >= 1.2 and signed > 0
        detail = f"бид/аск {ratio:.2f}, дельта {'+' if signed >= 0 else '−'}"
    else:
        ok = ratio <= 1 / 1.2 and signed < 0
        detail = f"бид/аск {ratio:.2f}, дельта {'+' if signed >= 0 else '−'}"
    return (1 if ok else 0), detail


def _volume_score(ctx: dict[str, Any]) -> tuple[int, float]:
    df5 = ctx["df5"]
    base = float(df5["volume"].iloc[-21:-1].mean()) if len(df5) > 21 else 0.0
    ratio = float(df5["volume"].iloc[-1]) / base if base > 0 else 0.0
    return (1 if ratio >= 1.25 else 0), ratio


def _funding_pct(metrics: dict[str, Any], intervals: float) -> float:
    rate = _f(metrics.get("funding_rate"), 0.0) or 0.0
    return abs(rate) * 100.0 * max(0.0, float(intervals))


def _fees_pct(ctx: dict[str, Any]) -> tuple[float, float]:
    funding = _funding_pct(ctx["metrics"], ctx["funding_intervals"])
    return FEE_ROUND_TRIP * 100.0, funding


def _targets(
    ctx: dict[str, Any],
    side: str,
    entry: float,
    sl: float,
) -> tuple[float | None, float | None, str]:
    risk = abs(entry - sl)
    if risk <= 0:
        return None, None, ""
    levels_high = [x for x in (ctx["swing_highs"] + [ctx["range_high"], ctx["vwap"]]) if x > entry]
    levels_low = [x for x in (ctx["swing_lows"] + [ctx["range_low"], ctx["vwap"]]) if x < entry]
    if side == "long":
        levels = sorted(levels_high)
        candidates = [x for x in levels if x >= entry + 0.8 * risk]
        if not candidates:
            return entry + 2.0 * risk, entry + 3.0 * risk, ""
        first = candidates[0]
        tp1 = min(first, entry + 3.0 * risk)
        rest = [x for x in candidates if x > tp1 * 1.0005]
        tp2 = min(rest) if rest else entry + 3.0 * risk
        source = "уровень сверху" if first <= entry + 3.0 * risk else "R/R 2.0"
        return tp1, max(tp2, tp1 + 0.25 * risk), source
    levels = sorted(levels_low, reverse=True)
    candidates = [x for x in levels if x <= entry - 0.8 * risk]
    if not candidates:
        return entry - 2.0 * risk, entry - 3.0 * risk, ""
    first = candidates[0]
    tp1 = max(first, entry - 3.0 * risk)
    rest = [x for x in candidates if x < tp1 * 0.9995]
    tp2 = max(rest) if rest else entry - 3.0 * risk
    source = "уровень снизу" if first >= entry - 3.0 * risk else "R/R 2.0"
    return tp1, min(tp2, tp1 - 0.25 * risk), source


def _rr(entry: float, sl: float, tp: float, cost_pct: float) -> tuple[float, float, float]:
    if entry <= 0 or sl <= 0 or tp <= 0 or sl == entry:
        return 0.0, 0.0, 0.0
    risk_pct = abs(entry - sl) / entry
    tp_pct = abs(tp - entry) / entry
    cost = max(0.0, float(cost_pct)) / 100.0
    gross = tp_pct / risk_pct if risk_pct > 0 else 0.0
    net = (tp_pct - cost) / (risk_pct + cost) if (risk_pct + cost) > 0 else 0.0
    return risk_pct * 100.0, gross, net


def _filter(
    fid: str,
    label: str,
    ok: bool,
    *,
    value: Any = None,
    limit: Any = None,
    text: str = "",
    reason: str = "",
) -> dict[str, Any]:
    return {
        "id": fid,
        "label": label,
        "ok": bool(ok),
        "value": value,
        "limit": limit,
        "text": text,
        "reason": reason,
    }


def _common_filters(
    ctx: dict[str, Any],
    *,
    spread_pct: float | None,
    depth_usdt: float | None,
    turnover: float | None,
    max_spread_pct: float,
    min_depth_usdt: float,
    min_turnover_usdt: float,
) -> list[dict[str, Any]]:
    spread_ok = spread_pct is not None and spread_pct <= max_spread_pct
    depth_ok = depth_usdt is not None and depth_usdt >= min_depth_usdt
    turnover_ok = turnover is not None and turnover >= min_turnover_usdt
    return [
        _filter(
            "spread",
            "Спред",
            spread_ok,
            value=round(spread_pct, 4) if spread_pct is not None else None,
            limit=max_spread_pct,
            text=(
                f"{spread_pct:.3f}% ≤ {max_spread_pct:.3f}%"
                if spread_pct is not None
                else "нет данных"
            ),
            reason=(
                f"спред {spread_pct:.3f}% > {max_spread_pct:.3f}%"
                if spread_pct is not None
                else "нет данных стакана для спреда"
            ),
        ),
        _filter(
            "depth",
            "Глубина",
            depth_ok,
            value=round(depth_usdt, 0) if depth_usdt is not None else None,
            limit=min_depth_usdt,
            text=(
                f"${depth_usdt:,.0f} ≥ ${min_depth_usdt:,.0f}"
                if depth_usdt is not None
                else "нет данных"
            ),
            reason=(
                f"глубина ${depth_usdt:,.0f} < ${min_depth_usdt:,.0f}"
                if depth_usdt is not None
                else "нет данных стакана для глубины"
            ),
        ),
        _filter(
            "turnover",
            "Оборот 24ч",
            turnover_ok,
            value=round(turnover, 0) if turnover is not None else None,
            limit=min_turnover_usdt,
            text=(
                f"${turnover / 1_000_000:.1f}M ≥ ${min_turnover_usdt / 1_000_000:.1f}M"
                if turnover is not None
                else "нет данных"
            ),
            reason=(
                f"оборот ${turnover / 1_000_000:.1f}M < ${min_turnover_usdt / 1_000_000:.1f}M"
                if turnover is not None
                else "нет данных по обороту"
            ),
        ),
    ]


def _score_parts(
    htf: int, level: int, trigger: int, volume: int, flow: int
) -> list[dict[str, Any]]:
    return [
        {"id": "htf", "label": "Старший ТФ", "score": htf, "max": 2},
        {"id": "level", "label": "Уровень / VWAP", "score": level, "max": 2},
        {"id": "trigger", "label": "Триггер", "score": trigger, "max": 2},
        {"id": "volume", "label": "Объём", "score": volume, "max": 1},
        {"id": "flow", "label": "Поток", "score": flow, "max": 1},
    ]


def _base_card(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair": ctx["pair"],
        "price": round(float(ctx["close"]), 8),
        "regime": ctx["regime"],
        "timeframes": ctx["timeframes"],
        "score_max": 8,
        "fees_pct": round(FEE_ROUND_TRIP * 100.0, 4),
        "fees_usdt_1000": round(FEE_USDT_PER_1000, 2),
    }


def _finalize(
    ctx: dict[str, Any],
    *,
    setup: str,
    side: str,
    parts: list[dict[str, Any]],
    entry: float | None,
    sl: float | None,
    tp1: float | None,
    tp2: float | None,
    filters: list[dict[str, Any]],
    rationale: list[str],
    min_score: int,
    min_rr: float,
) -> dict[str, Any]:
    score = int(sum(int(p["score"]) for p in parts))
    fees_pct, funding_pct = _fees_pct(ctx)
    risk_pct, rr_gross, rr_net = _rr(entry or 0.0, sl or 0.0, tp1 or 0.0, fees_pct + funding_pct)
    score_ok = score >= int(min_score)
    rr_ok = rr_net >= float(min_rr)
    filters = [
        *filters,
        _filter(
            "risk",
            "Риск на сделку",
            entry is not None and sl is not None and risk_pct >= ctx["min_risk_pct"]
            and risk_pct <= ctx["max_risk_pct"],
            value=round(risk_pct, 3),
            limit=[ctx["min_risk_pct"], ctx["max_risk_pct"]],
            text=(
                f"{risk_pct:.2f}% в пределах "
                f"{ctx['min_risk_pct']:.2f}–{ctx['max_risk_pct']:.2f}%"
                if entry is not None and sl is not None
                else "нет SL"
            ),
            reason=(
                f"стоп {risk_pct:.2f}% вне диапазона "
                f"{ctx['min_risk_pct']:.2f}–{ctx['max_risk_pct']:.2f}%"
                if entry is not None and sl is not None
                else "не рассчитан стоп"
            ),
        ),
        _filter(
            "rr",
            "R/R после комиссий",
            rr_ok,
            value=round(rr_net, 3),
            limit=float(min_rr),
            text=f"{rr_net:.2f} ≥ {float(min_rr):.2f}",
            reason=f"R/R {rr_net:.2f} < {float(min_rr):.2f} после комиссий и фандинга",
        ),
        _filter(
            "score",
            "Скор",
            score_ok,
            value=score,
            limit=int(min_score),
            text=f"{score}/8 ≥ {int(min_score)}/8",
            reason=f"скор {score}/8 ниже порога {int(min_score)}/8",
        ),
    ]
    failed = [f for f in filters if not f["ok"]]
    passed = not failed
    decision = side.upper() if passed and side else "NO_TRADE"
    reason = ""
    if decision == "NO_TRADE":
        reason = failed[0]["reason"] if failed else "нет подтверждённого сетапа"
    card = _base_card(ctx)
    card.update(
        {
            "setup": setup,
            "setup_label": SETUP_LABELS.get(setup, setup),
            "tag": TAGS.get(setup, ""),
            "decision": decision,
            "side": side if decision != "NO_TRADE" else "",
            "score": score,
            "score_parts": parts,
            "entry": round(float(entry), 8) if entry is not None else None,
            "sl": round(float(sl), 8) if sl is not None else None,
            "tp1": round(float(tp1), 8) if tp1 is not None else None,
            "tp2": round(float(tp2), 8) if tp2 is not None else None,
            "risk_pct": round(float(risk_pct), 3),
            "rr_gross": round(float(rr_gross), 2),
            "rr_net": round(float(rr_net), 2),
            "funding_pct": round(float(funding_pct), 4),
            "spread_pct": round(float(ctx["spread_pct"] or 0.0), 4),
            "depth_usdt": round(float(ctx["depth_usdt"] or 0.0), 0),
            "turnover24h": round(float(ctx["turnover24h"] or 0.0), 0),
            "volume_ratio": round(float(ctx["volume_ratio"]), 2),
            "filters": filters,
            "reasons": rationale,
            "reason": reason,
        }
    )
    return card


def _empty_card(ctx: dict[str, Any], reason: str) -> dict[str, Any]:
    card = _base_card(ctx)
    card.update(
        {
            "setup": "",
            "setup_label": "—",
            "tag": "",
            "decision": "NO_TRADE",
            "side": "",
            "score": 0,
            "score_parts": [],
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "risk_pct": 0.0,
            "rr_gross": 0.0,
            "rr_net": 0.0,
            "funding_pct": 0.0,
            "spread_pct": round(float(ctx["spread_pct"] or 0.0), 4),
            "depth_usdt": round(float(ctx["depth_usdt"] or 0.0), 0),
            "turnover24h": round(float(ctx["turnover24h"] or 0.0), 0),
            "volume_ratio": 0.0,
            "filters": [],
            "reasons": [],
            "reason": reason,
        }
    )
    return card


def _trend_pullback(ctx: dict[str, Any]) -> dict[str, Any] | None:
    regime = ctx["regime"]["key"]
    side = "long" if ctx["regime"]["trend_dir"] > 0 else "short" if ctx["regime"]["trend_dir"] < 0 else ""
    if not side:
        return None
    if regime == "risky":
        htf = 0
    else:
        htf = _htf_score(ctx, side)
    close = ctx["close"]
    atr5 = ctx["atr_5m"] or 0.0
    if atr5 <= 0:
        return None
    tolerance = 0.45 * atr5
    vwap = ctx["vwap"] or close
    ema20 = ctx["ema20_5m"] or close
    ema50 = ctx["ema50_5m"] or close
    ema15 = ctx["ema20_15m"] or close
    swing_highs = ctx["swing_highs"]
    swing_lows = ctx["swing_lows"]

    if side == "long":
        sources = [("VWAP", vwap), ("EMA20 5m", ema20), ("EMA20 15m", ema15)]
        below = [x for x in swing_highs if close * 0.97 <= x < close]
        if below:
            sources.append(("Пробой уровня", max(below)))
    else:
        sources = [("VWAP", vwap), ("EMA20 5m", ema20), ("EMA20 15m", ema15)]
        above = [x for x in swing_lows if close < x <= close * 1.03]
        if above:
            sources.append(("Пробой уровня", min(above)))
    sources = [(name, float(level)) for name, level in sources if level and level > 0]
    if not sources:
        return None
    nearest_name, nearest_level = min(sources, key=lambda item: abs(close - item[1]))
    distance = abs(close - nearest_level)
    zone_sources = [name for name, level in sources if abs(level - nearest_level) <= 0.35 * atr5]
    near_level = distance <= tolerance
    touched = False
    for i in range(1, min(5, len(ctx["df5"]))):
        bar = ctx["df5"].iloc[-i]
        if side == "long":
            if float(bar["low"]) <= nearest_level + 0.2 * atr5 and float(bar["close"]) >= nearest_level - 0.2 * atr5:
                touched = True
        else:
            if float(bar["high"]) >= nearest_level - 0.2 * atr5 and float(bar["close"]) <= nearest_level + 0.2 * atr5:
                touched = True
    level_score = 2 if near_level and touched and len(zone_sources) >= 2 else 1 if near_level or touched else 0

    last_bar = ctx["df5"].iloc[-1]
    bar_range = float(last_bar["high"]) - float(last_bar["low"])
    body_ratio = abs(float(last_bar["close"]) - float(last_bar["open"])) / bar_range if bar_range > 0 else 0.0
    if side == "long":
        direction_ok = float(last_bar["close"]) > float(last_bar["open"]) and body_ratio >= 0.3
        reclaim = float(last_bar["close"]) > nearest_level
    else:
        direction_ok = float(last_bar["close"]) < float(last_bar["open"]) and body_ratio >= 0.3
        reclaim = float(last_bar["close"]) < nearest_level
    confirm = _one_minute_confirm(ctx.get("df1m"), side)
    trigger_full = bool(touched and direction_ok and reclaim)
    trigger_score = 2 if trigger_full and confirm else 1 if trigger_full or (touched and confirm) else 0

    volume_score, _ = _volume_score(ctx)
    flow_score, flow_note = _flow_score(ctx, side)

    entry = close
    recent = ctx["df5"].iloc[-12:]
    if side == "long":
        pull_start = int(np.argmax(recent["high"].values))
        leg = recent.iloc[pull_start:] if pull_start < len(recent) - 1 else recent.iloc[-6:]
        sl = float(leg["low"].min()) - 0.25 * atr5
        if sl >= entry:
            sl = entry - 0.5 * atr5
    else:
        pull_start = int(np.argmin(recent["low"].values))
        leg = recent.iloc[pull_start:] if pull_start < len(recent) - 1 else recent.iloc[-6:]
        sl = float(leg["high"].max()) + 0.25 * atr5
        if sl <= entry:
            sl = entry + 0.5 * atr5
    tp1, tp2, tp_source = _targets(ctx, side, entry, sl)

    filters = _common_filters(
        ctx,
        spread_pct=ctx["spread_pct"],
        depth_usdt=ctx["depth_usdt"],
        turnover=ctx["turnover24h"],
        max_spread_pct=ctx["max_spread_pct"],
        min_depth_usdt=ctx["min_depth_usdt"],
        min_turnover_usdt=ctx["min_turnover_usdt"],
    )
    chase_ok = distance <= max(0.8 * atr5, 0.0035 * entry)
    filters.append(
        _filter(
            "chase",
            "Не догонять",
            chase_ok,
            value=round(distance / atr5, 2) if atr5 else None,
            limit=0.8,
            text=f"{distance / atr5:.2f} ATR от уровня" if atr5 else "—",
            reason="цена ушла далеко от уровня входа — догонять поздно",
        )
    )
    regime_ok = regime in ("trend_up", "trend_down", "range") and not (
        (side == "long" and regime == "trend_down") or (side == "short" and regime == "trend_up")
    )
    if regime in ("impulse_up", "impulse_down", "risky"):
        regime_ok = False
    filters.append(
        _filter(
            "regime",
            "Режим",
            regime_ok,
            value=regime,
            limit="trend_up/trend_down",
            text=ctx["regime"]["label"],
            reason=f"режим «{ctx['regime']['label']}» не подходит для отката",
        )
    )
    if tp1 is None or tp2 is None:
        room_ok = False
        room_text = "нет цели"
    else:
        risk = abs(entry - sl)
        room_ok = abs(tp1 - entry) >= 1.2 * risk
        room_text = f"TP1 {abs(tp1 - entry) / risk:.2f}R" if risk > 0 else "—"
    filters.append(
        _filter(
            "room",
            "Место до цели",
            room_ok,
            value=room_text,
            limit="≥ 1.2R",
            text=room_text,
            reason="мало места до ближайшей ликвидности (< 1.2R)",
        )
    )
    rationale = [
        f"1h: {ctx['timeframes']['1h']}, ADX {ctx['regime']['adx_1h']:.1f}",
        f"15m: {ctx['timeframes']['15m']}",
        f"5m: откат к {nearest_name} {nearest_level:.6g}"
        + (", свеча закрылась в сторону тренда" if trigger_full else ", триггер не подтверждён"),
        f"R/R {(abs(tp1 - entry) / abs(entry - sl)) if tp1 and sl != entry else 0:.2f} до {tp_source or 'цели'}",
        flow_note,
    ]
    return _finalize(
        ctx,
        setup="trend_pullback",
        side=side,
        parts=_score_parts(htf, level_score, trigger_score, volume_score, flow_score),
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        filters=filters,
        rationale=rationale,
        min_score=ctx["min_score"],
        min_rr=ctx["min_rr"],
    )


def _liquidity_sweep(ctx: dict[str, Any], side: str) -> dict[str, Any] | None:
    df5 = ctx["df5"]
    if len(df5) < 50:
        return None
    last = df5.iloc[-1]
    prior_window = df5.iloc[-50:-2]
    atr5 = ctx["atr_5m"] or 0.0
    if atr5 <= 0:
        return None
    close = ctx["close"]
    if side == "long":
        prior = float(prior_window["low"].min())
        swept = float(last["low"]) < prior and close > prior
        wick = min(float(last["open"]), close) - float(last["low"])
        reclaim = close > float(last["open"]) and close > prior
        extreme = float(last["low"])
    else:
        prior = float(prior_window["high"].max())
        swept = float(last["high"]) > prior and close < prior
        wick = float(last["high"]) - max(float(last["open"]), close)
        reclaim = close < float(last["open"]) and close < prior
        extreme = float(last["high"])
    if not swept or not reclaim:
        return None
    bar_range = float(last["high"]) - float(last["low"])
    wick_ratio = wick / bar_range if bar_range > 0 else 0.0
    wick_ok = wick_ratio >= 0.35
    sweep_dist = abs(extreme - prior) / atr5
    if sweep_dist > 1.2:
        return None
    touches = _touches(df5, prior)
    range_low = ctx["range_low"]
    range_high = ctx["range_high"]
    at_boundary = (
        abs(prior - range_low) / atr5 <= 1.0 if side == "long" else abs(prior - range_high) / atr5 <= 1.0
    )
    level_score = 2 if touches >= 2 and at_boundary else 1 if touches >= 1 else 0
    volume_score, _ = _volume_score(ctx)
    volume_ratio = ctx["volume_ratio"]
    confirm = _one_minute_confirm(ctx.get("df1m"), side)
    trigger_full = bool(wick_ok and volume_ratio >= 1.15)
    trigger_score = 2 if trigger_full and confirm else 1 if trigger_full or (reclaim and confirm) else 0
    htf = _sweep_htf_score(ctx, side)
    flow_score, flow_note = _flow_score(ctx, side)

    entry = close
    buffer = 0.25 * atr5
    sl = extreme - buffer if side == "long" else extreme + buffer
    tp1, tp2, tp_source = _targets(ctx, side, entry, sl)
    filters = _common_filters(
        ctx,
        spread_pct=ctx["spread_pct"],
        depth_usdt=ctx["depth_usdt"],
        turnover=ctx["turnover24h"],
        max_spread_pct=ctx["max_spread_pct"],
        min_depth_usdt=ctx["min_depth_usdt"],
        min_turnover_usdt=ctx["min_turnover_usdt"],
    )
    regime = ctx["regime"]["key"]
    strong_against = (side == "long" and regime in ("trend_down", "impulse_down")) or (
        side == "short" and regime in ("trend_up", "impulse_up")
    )
    regime_ok = not strong_against and regime != "risky"
    filters.append(
        _filter(
            "regime",
            "Режим",
            regime_ok,
            value=regime,
            limit="range / слабый тренд",
            text=ctx["regime"]["label"],
            reason="сильный тренд против разворота — пробой может продолжиться",
        )
    )
    if tp1 is None or tp2 is None:
        room_ok = False
        room_text = "нет цели"
    else:
        risk = abs(entry - sl)
        room_ok = abs(tp1 - entry) >= 1.2 * risk
        room_text = f"TP1 {abs(tp1 - entry) / risk:.2f}R" if risk > 0 else "—"
    filters.append(
        _filter(
            "room",
            "Место до цели",
            room_ok,
            value=room_text,
            limit="≥ 1.2R",
            text=room_text,
            reason="мало места до VWAP/границы ренджа (< 1.2R)",
        )
    )
    rationale = [
        f"1h: {ctx['timeframes']['1h']}, режим {ctx['regime']['label']}",
        f"15m: {ctx['timeframes']['15m']}",
        f"5m: прострел {'минимума' if side == 'long' else 'максимума'} {prior:.6g} "
        f"и возврат внутрь, тень {wick_ratio * 100:.0f}%",
        f"уровень касался {touches} раз" + (", граница ренджа" if at_boundary else ""),
        f"R/R {(abs(tp1 - entry) / abs(entry - sl)) if tp1 and sl != entry else 0:.2f} до {tp_source or 'цели'}",
        flow_note,
    ]
    return _finalize(
        ctx,
        setup="liquidity_sweep",
        side=side,
        parts=_score_parts(htf, level_score, trigger_score, volume_score, flow_score),
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        filters=filters,
        rationale=rationale,
        min_score=ctx["min_score"],
        min_rr=ctx["min_rr"],
    )


def _choose(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cards:
        return None

    def rank(card: dict[str, Any]) -> tuple[int, int, float]:
        return (
            1 if card.get("decision") != "NO_TRADE" else 0,
            int(card.get("score") or 0),
            float(card.get("rr_net") or 0.0),
        )

    return max(cards, key=rank)


def _timeframes(ctx: dict[str, Any]) -> dict[str, str]:
    dir1 = ctx["regime"]["trend_dir"]
    close = ctx["close"]
    close15 = ctx["close_15m"]
    if dir1 > 0:
        tf1 = "↑ тренд"
    elif dir1 < 0:
        tf1 = "↓ тренд"
    else:
        tf1 = "→ флэт"
    if ctx["ema20_15m"] and close15 > ctx["ema20_15m"]:
        tf15 = "↑ выше EMA20"
    elif ctx["ema20_15m"]:
        tf15 = "↓ ниже EMA20"
    else:
        tf15 = "—"
    last = ctx["df5"].iloc[-1]
    if float(last["close"]) > float(last["open"]):
        tf5 = "↑ бычья"
    elif float(last["close"]) < float(last["open"]):
        tf5 = "↓ медвежья"
    else:
        tf5 = "→ доджи"
    if ctx["vwap"]:
        tf5 += f", VWAP {ctx['vwap']:.6g}"
    return {"1h": tf1, "15m": tf15, "5m": tf5}


def analyze_pair(
    pair: str,
    frames: dict[str, pd.DataFrame | None],
    metrics: dict[str, Any] | None = None,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    min_rr: float = DEFAULT_MIN_RR,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_depth_usdt: float = DEFAULT_MIN_DEPTH_USDT,
    min_turnover_usdt: float = DEFAULT_MIN_TURNOVER_USDT,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    min_risk_pct: float = DEFAULT_MIN_RISK_PCT,
    funding_intervals: float = DEFAULT_FUNDING_INTERVALS,
) -> dict[str, Any]:
    """Build one signal card for a pair. Always returns a card (NO_TRADE on gaps)."""
    metrics = metrics or {}
    df5 = _prep(frames.get("5m"))
    df15 = _prep(frames.get("15m"))
    df1h = _prep(frames.get("1h"))
    df1m = _prep(frames.get("1m"))
    if df5 is None or df15 is None or df1h is None or len(df5) < 60 or len(df15) < 50 or len(df1h) < 50:
        return {
            "pair": pair,
            "decision": "NO_TRADE",
            "reason": "мало истории свечей для анализа",
            "score": 0,
            "score_max": 8,
            "score_parts": [],
            "filters": [
                _filter("data", "Данные", False, reason="мало истории свечей для анализа")
            ],
            "reasons": [],
            "setup": "",
            "setup_label": "—",
            "tag": "",
            "side": "",
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "rr_net": 0.0,
            "rr_gross": 0.0,
            "risk_pct": 0.0,
            "spread_pct": 0.0,
            "depth_usdt": 0.0,
            "turnover24h": 0.0,
            "fees_pct": round(FEE_ROUND_TRIP * 100.0, 4),
            "fees_usdt_1000": round(FEE_USDT_PER_1000, 2),
            "regime": {"key": "unknown", "label": "Нет данных", "adx_1h": 0, "adx_15m": 0, "atr_pct_1h": 0},
            "timeframes": {"1h": "—", "15m": "—", "5m": "—"},
            "volume_ratio": 0.0,
            "price": _f(metrics.get("last"), 0.0) or 0.0,
        }
    close = float(df5["close"].iloc[-1])
    close15 = float(df15["close"].iloc[-1])
    close1h = float(df1h["close"].iloc[-1])
    atr5 = _last(_atr(df5, 14)) or 0.0
    atr15 = _last(_atr(df15, 14)) or 0.0
    atr1h = _last(_atr(df1h, 14)) or 0.0
    vwap = _last(_session_vwap(df5))
    swing_highs, swing_lows = _swings(df5)
    recent_range = df5.iloc[-40:]
    range_high = float(recent_range["high"].max())
    range_low = float(recent_range["low"].min())
    spread_pct = _f(metrics.get("spread_pct"), None)
    bid = _f(metrics.get("bid"), None)
    ask = _f(metrics.get("ask"), None)
    if spread_pct is None and bid and ask and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else None
    bid_depth = _f(metrics.get("bid_depth_usdt"), None)
    ask_depth = _f(metrics.get("ask_depth_usdt"), None)
    depth = min(bid_depth, ask_depth) if bid_depth is not None and ask_depth is not None else None
    turnover = _f(metrics.get("turnover24h"), None)
    volume_ratio = 0.0
    if len(df5) > 21:
        base_vol = float(df5["volume"].iloc[-21:-1].mean())
        volume_ratio = float(df5["volume"].iloc[-1]) / base_vol if base_vol > 0 else 0.0
    ctx: dict[str, Any] = {
        "pair": pair,
        "df5": df5,
        "df15": df15,
        "df1h": df1h,
        "df1m": df1m,
        "close": close,
        "close_15m": close15,
        "close_1h": close1h,
        "metrics": metrics,
        "spread_pct": spread_pct,
        "bid_depth_usdt": bid_depth,
        "ask_depth_usdt": ask_depth,
        "depth_usdt": depth,
        "turnover24h": turnover,
        "funding_intervals": funding_intervals,
        "atr_5m": atr5,
        "atr_15m": atr15,
        "atr_1h": atr1h,
        "ema20_5m": _last(_ema(df5["close"], 20)),
        "ema50_5m": _last(_ema(df5["close"], 50)),
        "ema20_15m": _last(_ema(df15["close"], 20)),
        "ema50_15m": _last(_ema(df15["close"], 50)),
        "ema20_1h": _last(_ema(df1h["close"], 20)),
        "ema50_1h": _last(_ema(df1h["close"], 50)),
        "adx_1h": _last(_adx(df1h, 14)),
        "adx_15m": _last(_adx(df15, 14)),
        "vwap": vwap,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "range_high": range_high,
        "range_low": range_low,
        "volume_ratio": volume_ratio,
        "min_score": int(min_score),
        "min_rr": float(min_rr),
        "max_spread_pct": float(max_spread_pct),
        "min_depth_usdt": float(min_depth_usdt),
        "min_turnover_usdt": float(min_turnover_usdt),
        "max_risk_pct": float(max_risk_pct),
        "min_risk_pct": float(min_risk_pct),
    }
    ctx["regime"] = _regime(ctx)
    ctx["timeframes"] = _timeframes(ctx)
    cards = [
        card
        for card in (
            _trend_pullback(ctx),
            _liquidity_sweep(ctx, "long"),
            _liquidity_sweep(ctx, "short"),
        )
        if card is not None
    ]
    chosen = _choose(cards)
    if chosen is None:
        return _empty_card(ctx, "нет сетапа: нет тренда с откатом и нет прострела уровня")
    chosen["alternatives"] = [
        {
            "setup": card.get("setup"),
            "setup_label": card.get("setup_label"),
            "decision": card.get("decision"),
            "side": card.get("side"),
            "score": card.get("score"),
            "reason": card.get("reason"),
        }
        for card in cards
        if card is not chosen
    ]
    if chosen.get("decision") == "NO_TRADE":
        chosen["reason"] = f"{chosen.get('setup_label')}: {chosen.get('reason')}"
    return chosen
