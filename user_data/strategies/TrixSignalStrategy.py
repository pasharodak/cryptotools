# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_trix."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import TrixSignalStrategy as _Sim


class TrixSignalStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
