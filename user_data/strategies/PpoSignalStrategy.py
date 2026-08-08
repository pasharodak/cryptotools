# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_ppo."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import PpoSignalStrategy as _Sim


class PpoSignalStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
