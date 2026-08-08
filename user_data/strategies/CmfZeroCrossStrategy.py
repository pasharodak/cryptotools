# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_cmf."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import CmfZeroCrossStrategy as _Sim


class CmfZeroCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
