# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim scalp_ema."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimScalpingStrategies import ScalpEmaCrossStrategy as _Sim


class ScalpEmaCrossStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
