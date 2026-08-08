# pragma pylint: disable=missing-docstring, invalid-name
"""Fib pullback — prod alias for sim trend_fib (SimFibPullback)."""

from _sim_live import SimLiveCooldownMixin, SimLiveSlTpMixin
from simulation.strategies.SimTrendStrategies import SimFibPullback


class FibPullbackStrategy(SimLiveSlTpMixin, SimLiveCooldownMixin, SimFibPullback):
    pass
