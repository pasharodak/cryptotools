# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_obv."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import ObvEmaCrossStrategy as _Sim


class ObvEmaCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
