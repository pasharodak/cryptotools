# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart_adxdi."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaStrategies import AdxDiCrossStrategy as _Sim


class AdxDiCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
