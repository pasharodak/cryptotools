# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_hma_ppo_atr."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import HmaPpoAtrComboStrategy as _Sim


class HmaPpoAtrComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
