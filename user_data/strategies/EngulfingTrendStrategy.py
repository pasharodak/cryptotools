# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_engulf."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import EngulfingTrendStrategy as _Sim


class EngulfingTrendStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
