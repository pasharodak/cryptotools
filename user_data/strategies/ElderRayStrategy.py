# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_elder."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import ElderRayStrategy as _Sim


class ElderRayStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
