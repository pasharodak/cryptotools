# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_kc_stoch_vol."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import KeltnerStochVolComboStrategy as _Sim


class KeltnerStochVolComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
