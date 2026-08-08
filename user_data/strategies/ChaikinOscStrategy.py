# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart3_adosc."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave3Strategies import ChaikinOscStrategy as _Sim


class ChaikinOscStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
