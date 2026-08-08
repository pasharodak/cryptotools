# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_tema."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import TemaCrossStrategy as _Sim


class TemaCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
