# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim new_ichimoku."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimNewSetStrategies import IchimokuTkCrossStrategy as _Sim


class IchimokuTkCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
