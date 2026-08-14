# pragma pylint: disable=missing-docstring, invalid-name
"""Range BB bounce — prod alias for sim lite_range."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.LiteFinanceStrategies import LiteRangeStrategy as _SimLiteRange


class LiteRangeStrategy(SimLiveCooldownMixin, _SimLiteRange):
    pass
