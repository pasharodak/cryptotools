"""Grid for sim/ML export — prod parity (VolatilityGridStrategy + config_grid)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from user_data.strategies.VolatilityGridStrategy import VolatilityGridStrategy  # noqa: E402


class SimVolatilityGridAggressive(VolatilityGridStrategy):
    """Same SL/TP/partials/leverage as live grid bot — no sim-only overrides."""

    pass
