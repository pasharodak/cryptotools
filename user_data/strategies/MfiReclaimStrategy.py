# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart2_mfi."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaWave2Strategies import MfiReclaimStrategy as _Sim


class MfiReclaimStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
