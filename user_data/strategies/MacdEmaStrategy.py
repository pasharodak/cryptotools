# pragma pylint: disable=missing-docstring, invalid-name
"""MACD + EMA200 — prod alias for sim trend_macd_ema (SimMacdEma)."""

from _sim_live import SimLiveCooldownMixin
from simulation.strategies.SimClassicStrategies import SimMacdEma


class MacdEmaStrategy(SimLiveCooldownMixin, SimMacdEma):
    pass
