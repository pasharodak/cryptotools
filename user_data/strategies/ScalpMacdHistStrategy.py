# pragma pylint: disable=missing-docstring, invalid-name
"""Scalp MACD hist — prod alias for sim scalp_macd."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimScalpingStrategies import ScalpMacdHistStrategy as _Sim


class ScalpMacdHistStrategy(SimLiveCooldownMixin, _Sim):
    pass
