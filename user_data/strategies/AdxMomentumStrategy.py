# pragma pylint: disable=missing-docstring, invalid-name
"""Breakout-Retest — prod alias for sim trend_breakout (SimBreakoutRetest)."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimTrendStrategies import SimBreakoutRetest


class AdxMomentumStrategy(SimLiveCooldownMixin, SimBreakoutRetest):
    pass
