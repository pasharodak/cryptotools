# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_ema_rsi_atr."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import EmaRsiAtrComboStrategy as _Sim


class EmaRsiAtrComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
