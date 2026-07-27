"""Simulation-only grid — balanced entries + multi-pair activity for player replay."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from user_data.strategies.VolatilityGridStrategy import VolatilityGridStrategy  # noqa: E402


class SimVolatilityGridPlayer(VolatilityGridStrategy):
    """Player replay: live-like filters, moderate cooldown, partial TP."""

    adx_max = 18
    bb_width_min = 0.025
    partial_tp_profit = 0.005
    pair_cooldown_minutes = 360
    max_entry_position_adjustment = 0
