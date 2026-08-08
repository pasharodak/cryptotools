# pragma pylint: disable=missing-docstring, invalid-name
"""Alt volume breakout — prod alias for sim scalp_liq_breakout."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimLiquidityScalpStrategies import AltVolumeBreakoutStrategy as _Sim


class AltVolumeBreakoutStrategy(SimLiveCooldownMixin, _Sim):
    pass
