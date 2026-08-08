# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_atrch."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import AtrChannelBreakoutStrategy as _Sim


class AtrChannelBreakoutStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
