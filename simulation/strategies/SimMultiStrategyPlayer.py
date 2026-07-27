"""Sim wrapper — MultiStrategyRouter with configurable leverage."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_UD = _ROOT / "user_data" / "strategies"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_UD) not in sys.path:
    sys.path.insert(0, str(_UD))

from user_data.strategies.MultiStrategyRouter import MultiStrategyRouter  # noqa: E402


class SimMultiStrategyPlayer(MultiStrategyRouter):
    sim_leverage = 5.0

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag,
        side: str,
        **kwargs,
    ) -> float:
        return min(self.sim_leverage, max_leverage)
