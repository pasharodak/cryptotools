# pragma pylint: disable=missing-docstring, invalid-name
"""Keltner breakout — prod alias for sim new_keltner."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimNewSetStrategies import KeltnerBreakoutStrategy as _Sim


class KeltnerBreakoutStrategy(SimLiveCooldownMixin, _Sim):
    pass
