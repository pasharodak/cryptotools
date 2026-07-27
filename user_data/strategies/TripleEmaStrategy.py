# pragma pylint: disable=missing-docstring, invalid-name
"""EMA 50/200 (4H) — prod alias for sim trend_ema (SimEmaGoldenCross)."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimTrendStrategies import SimEmaGoldenCross


class TripleEmaStrategy(SimLiveCooldownMixin, SimEmaGoldenCross):
    pass
