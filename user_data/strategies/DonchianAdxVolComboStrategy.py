# pragma pylint: disable=missing-docstring, invalid-name
"""Prod alias for sim combo_don_adx_vol."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimComboStrategies import DonchianAdxVolComboStrategy as _Sim


class DonchianAdxVolComboStrategy(SimLiveCooldownMixin, _Sim):
    # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
    pass
