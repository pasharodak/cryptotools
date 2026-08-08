# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_vortex."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import VortexCrossStrategy as _Sim


class VortexCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
