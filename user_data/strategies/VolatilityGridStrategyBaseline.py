# pragma pylint: disable=missing-docstring, invalid-name
"""Volatility Grid — baseline params before optimization (Jun 25)."""

from VolatilityGridStrategy import VolatilityGridStrategy


class VolatilityGridStrategyBaseline(VolatilityGridStrategy):
    max_entry_position_adjustment = 1
    adx_max = 28
    bb_width_min = 0.018
    pair_cooldown_minutes = 120
