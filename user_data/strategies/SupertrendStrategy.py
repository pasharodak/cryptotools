# pragma pylint: disable=missing-docstring, invalid-name
"""Supertrend — prod alias for sim trend_supertrend (SimSupertrend)."""

from _sim_live import SimLiveCooldownMixin, SimLiveSlTpMixin
from simulation.strategies.SimClassicStrategies import SimSupertrend


class SupertrendStrategy(SimLiveSlTpMixin, SimLiveCooldownMixin, SimSupertrend):
    pass
