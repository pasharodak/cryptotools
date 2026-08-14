# pragma pylint: disable=missing-docstring, invalid-name
"""Supertrend — prod alias for sim trend_supertrend (SimSupertrend)."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimClassicStrategies import SimSupertrend


class SupertrendStrategy(SimLiveCooldownMixin, SimSupertrend):
    pass
