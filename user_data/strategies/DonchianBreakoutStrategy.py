# pragma pylint: disable=missing-docstring, invalid-name
"""Donchian / Turtle — prod alias for sim new_donchian."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimNewSetStrategies import DonchianBreakoutStrategy as _Sim


class DonchianBreakoutStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
