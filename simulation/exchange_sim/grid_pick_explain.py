"""Human-readable explanation why a ranging pair was picked for Bybit Grid."""
from __future__ import annotations

from typing import Any


def explain_grid_pick(best: dict[str, Any] | None, *, rank_note: str = "") -> str:
    """Build 2–4 Russian sentences from analyze_ranging metrics."""
    if not best:
        return "Пара ещё не выбрана — запустите скан."

    pair = best.get("pair") or best.get("symbol") or "—"
    score = best.get("score")
    adx = best.get("adx")
    bb = best.get("bb_width")
    ranging = best.get("ranging_ratio")
    inside = best.get("inside_bb_ratio")
    atr = best.get("atr_ratio")
    htf_ok = best.get("htf_ok")
    priority = best.get("priority")
    turnover = best.get("turnover24h")

    parts: list[str] = []
    head = f"Выбрана {pair}"
    if score is not None:
        head += f" со score {float(score):.4f}"
    head += " — лучший ranging-кандидат под нейтральный grid."
    if rank_note:
        head += f" {rank_note}"
    parts.append(head)

    trend_bits: list[str] = []
    if adx is not None:
        adx_f = float(adx)
        if adx_f <= 18:
            trend_bits.append(f"ADX {adx_f:.1f} низкий — рынок в боковике, тренд слабый")
        elif adx_f <= 25:
            trend_bits.append(f"ADX {adx_f:.1f} умеренный — допустимо для сетки")
        else:
            trend_bits.append(f"ADX {adx_f:.1f} повышен — тренд сильнее обычного для grid")
    if ranging is not None:
        trend_bits.append(f"доля ranging-свечей {float(ranging) * 100:.0f}%")
    if trend_bits:
        parts.append("; ".join(trend_bits) + ".")

    band_bits: list[str] = []
    if bb is not None:
        bb_f = float(bb)
        band_bits.append(f"ширина Bollinger {bb_f * 100:.2f}% — коридор для сеточных уровней")
    if inside is not None:
        band_bits.append(f"цена внутри BB {float(inside) * 100:.0f}% времени")
    if atr is not None:
        band_bits.append(f"ATR-ratio {float(atr):.2f}")
    if band_bits:
        parts.append("; ".join(band_bits) + ".")

    extras: list[str] = []
    if htf_ok is True:
        extras.append("старший ТФ подтверждает ranging")
    elif htf_ok is False:
        extras.append("старший ТФ слабее — score понижен")
    if priority:
        extras.append("пара из priority-списка (boost score)")
    if turnover is not None:
        extras.append(f"оборот 24ч ≈ {float(turnover) / 1e6:.1f}M USDT")
    if extras:
        parts.append("; ".join(extras) + ".")

    return " ".join(parts)


def metrics_summary(best: dict[str, Any] | None) -> dict[str, Any]:
    """Compact metrics for UI cards (no secrets)."""
    if not best:
        return {}
    keys = (
        "pair",
        "symbol",
        "score",
        "adx",
        "bb_width",
        "ranging_ratio",
        "inside_bb_ratio",
        "atr_ratio",
        "ema50_slope",
        "bb_width_pctile",
        "htf_ok",
        "priority",
        "turnover24h",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k in best and best[k] is not None:
            out[k] = best[k]
    return out
