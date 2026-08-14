# pragma pylint: disable=missing-docstring, invalid-name
"""Mean-reversion (BB) — prod alias for sim lite_mean_rev (SimMeanReversionRange)."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimMeanReversionRange import SimMeanReversionRange


class BollingerRsiStrategy(SimLiveCooldownMixin, SimMeanReversionRange):
    pass
