# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_roc."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import RocMomentumStrategy as _Sim


class RocMomentumStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
