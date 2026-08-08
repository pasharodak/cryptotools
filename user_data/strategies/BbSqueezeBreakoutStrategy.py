# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart_squeeze."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaStrategies import BbSqueezeBreakoutStrategy as _Sim


class BbSqueezeBreakoutStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
