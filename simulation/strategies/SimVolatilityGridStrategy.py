"""Simulation-only grid strategy — tighter filters for player replay."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from user_data.strategies.VolatilityGridStrategy import VolatilityGridStrategy  # noqa: E402


class SimVolatilityGridStrategy(VolatilityGridStrategy):
    """Tuned for sim player: stricter ranging, faster partial TP, longer cooldown."""

    adx_max = 18
    bb_width_min = 0.028
    partial_tp_profit = 0.005
    pair_cooldown_minutes = 360
    max_entry_position_adjustment = 0
