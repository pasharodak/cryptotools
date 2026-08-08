# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_st_rsi_obv."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import SupertrendRsiObvComboStrategy as _Sim


class SupertrendRsiObvComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
