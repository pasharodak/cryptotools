"""1-hour strategy ids, plus the stop/take percents the UI offers for every strategy.

1h signals are the parent strategy on closed 1h candles. Their catalog defaults
stay on the old 3/8/13 grid. Any strategy can store its own stop and take from
the wider preset list (same kind of menu as ML confidence).
"""

from __future__ import annotations

# Presets for the per-strategy menus. Stop stays inside the global 1–20% range.
# Take stays inside the global 2–50% range. 3/8/13 remain so saved 1h values
# do not move.
SL_CHOICES: tuple[int, ...] = (2, 3, 5, 8, 10, 13, 15, 20)
TP_CHOICES: tuple[int, ...] = (2, 3, 5, 8, 10, 13, 15, 20, 30, 50)
SLTP_CHOICES: tuple[int, ...] = SL_CHOICES

# Order matches the requested prod set.
HTF1H_SPECS: tuple[dict, ...] = (
    {"id": "AdxMomentum1hStrategy", "name": "Breakout-Retest", "num": 201, "sl": 13, "tp": 8, "scenario": "trend_breakout"},
    {"id": "MacdEma1hStrategy", "name": "MACD+EMA", "num": 202, "sl": 8, "tp": 3, "scenario": "trend_macd_ema"},
    {"id": "Supertrend1hStrategy", "name": "Supertrend", "num": 203, "sl": 8, "tp": 3, "scenario": "trend_supertrend"},
    {"id": "TripleEma1hStrategy", "name": "EMA 50/200", "num": 204, "sl": 8, "tp": 3, "scenario": "trend_ema"},
    {"id": "HeikinAshiFlip1hStrategy", "name": "Heikin Ashi", "num": 205, "sl": 8, "tp": 3, "scenario": "chart_ha"},
    {"id": "BollingerRsi1hStrategy", "name": "Mean-reversion", "num": 206, "sl": 8, "tp": 3, "scenario": "lite_mean_rev"},
    {"id": "ElderRay1hStrategy", "name": "Elder Ray", "num": 207, "sl": 3, "tp": 3, "scenario": "chart3_elder"},
    {"id": "PsaraFlip1hStrategy", "name": "Parabolic SAR", "num": 208, "sl": 3, "tp": 3, "scenario": "new_psar"},
    {"id": "AltVolumeBreakout1hStrategy", "name": "Volume breakout", "num": 209, "sl": 3, "tp": 3, "scenario": "scalp_liq_breakout"},
    {"id": "HmaPpoAtrCombo1hStrategy", "name": "HMA+PPO+ATR", "num": 210, "sl": 3, "tp": 3, "scenario": "combo_hma_ppo_atr"},
    {"id": "DonchianBreakout1hStrategy", "name": "Donchian", "num": 211, "sl": 3, "tp": 3, "scenario": "new_donchian"},
    {"id": "AtrChannelBreakout1hStrategy", "name": "ATR channel", "num": 212, "sl": 3, "tp": 3, "scenario": "chart3_atrch"},
    {"id": "DonchianAdxVolCombo1hStrategy", "name": "Donchian+ADX+объём", "num": 213, "sl": 3, "tp": 3, "scenario": "combo_don_adx_vol"},
    {"id": "CmfZeroCross1hStrategy", "name": "CMF", "num": 214, "sl": 3, "tp": 3, "scenario": "chart2_cmf"},
)

HTF1H_IDS = frozenset(spec["id"] for spec in HTF1H_SPECS)
HTF1H_BY_ID = {spec["id"]: spec for spec in HTF1H_SPECS}


def _snap_to(value, choices: tuple[int, ...]) -> int:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return int(choices[0])
    # Ratios (0.08) become percents. 1 means 1%, not 100%.
    if 0 < raw < 1:
        raw *= 100.0
    best = choices[0]
    best_dist = abs(raw - best)
    for choice in choices[1:]:
        dist = abs(raw - choice)
        if dist < best_dist:
            best = choice
            best_dist = dist
    return int(best)


def snap_sl_pct(value) -> int:
    """Nearest allowed stop percent."""
    return _snap_to(value, SL_CHOICES)


def snap_tp_pct(value) -> int:
    """Nearest allowed take-profit percent."""
    return _snap_to(value, TP_CHOICES)


def snap_sltp_pct(value) -> int:
    """Nearest allowed stop percent (legacy name)."""
    return snap_sl_pct(value)


def default_sltp(strategy_id: str) -> dict[str, int]:
    spec = HTF1H_BY_ID.get(strategy_id) or {}
    return {
        "sl": snap_sl_pct(spec.get("sl") or 5),
        "tp": snap_tp_pct(spec.get("tp") or 10),
    }


def sltp_ratios(strategy_id: str, stored: dict | None = None) -> tuple[float, float]:
    """Return (stoploss < 0, take-profit > 0) for one strategy."""
    base = default_sltp(strategy_id)
    src = stored if isinstance(stored, dict) else {}
    sl = snap_sl_pct(src.get("sl", base["sl"]))
    tp = snap_tp_pct(src.get("tp", base["tp"]))
    return -abs(sl) / 100.0, abs(tp) / 100.0
