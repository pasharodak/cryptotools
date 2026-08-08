# pragma pylint: disable=missing-docstring, invalid-name
"""Parabolic SAR flip — prod alias for sim new_psar."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimNewSetStrategies import PsaraFlipStrategy as _Sim


class PsaraFlipStrategy(SimLiveCooldownMixin, _Sim):
    pass
