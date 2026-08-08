# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim chart_willr."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimChartTaStrategies import WilliamsRReclaimStrategy as _Sim


class WilliamsRReclaimStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
