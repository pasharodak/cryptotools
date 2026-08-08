# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_ao."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import AwesomeOscStrategy as _Sim


class AwesomeOscStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
