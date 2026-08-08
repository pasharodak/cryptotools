# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_adx_macd_vol."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import AdxMacdVolComboStrategy as _Sim


class AdxMacdVolComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
