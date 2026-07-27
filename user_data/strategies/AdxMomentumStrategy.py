# pragma pylint: disable=missing-docstring, invalid-name
"""Breakout-Retest — prod alias for sim trend_breakout (SimBreakoutRetest)."""

from _sim_live import SimLiveCooldownMixin, SimLiveSlTpMixin
from simulation.strategies.SimTrendStrategies import SimBreakoutRetest


class AdxMomentumStrategy(SimLiveSlTpMixin, SimLiveCooldownMixin, SimBreakoutRetest):
    pass
