# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart_ha."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaStrategies import HeikinAshiFlipStrategy as _Sim


class HeikinAshiFlipStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
