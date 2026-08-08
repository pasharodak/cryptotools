# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_aroon."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import AroonCrossStrategy as _Sim


class AroonCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
