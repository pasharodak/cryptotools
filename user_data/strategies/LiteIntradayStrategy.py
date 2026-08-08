# pragma pylint: disable=missing-docstring, invalid-name
"""Intraday MACD — prod alias for sim lite_intraday."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.LiteFinanceStrategies import LiteIntradayStrategy as _SimLiteIntraday


class LiteIntradayStrategy(SimLiveCooldownMixin, _SimLiteIntraday):
    pass
