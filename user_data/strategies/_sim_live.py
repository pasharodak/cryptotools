"""Prod helpers — import simulation strategies with stable registry names."""
from __future__ import annotations

import sys
from pathlib import Path

from pandas import DataFrame

# Layouts:
# - monorepo: .../cryptotools/site/user_data/strategies/_sim_live.py
# - VPS app:  .../cryptotools/app/user_data/strategies/_sim_live.py
_STRAT_DIR = Path(__file__).resolve().parent
_USER_DATA = _STRAT_DIR.parent
_APP_OR_SITE = _USER_DATA.parent
_CANDIDATES = [
    _APP_OR_SITE / "simulation",  # VPS: app/simulation
    _APP_OR_SITE.parent / "simulation",  # monorepo: cryptotools/simulation
]
for _p in (_APP_OR_SITE, *(_c.parent for _c in _CANDIDATES if _c.is_dir()), *_CANDIDATES):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Grid / Finder: SL 3% · TP 6%
PROD_STOPLOSS = -0.03
PROD_TAKE_PROFIT = 0.06
PROD_MINIMAL_ROI = {"0": PROD_TAKE_PROFIT}

# Strategy bot (MultiStrategyRouter): SL 15% · TP 5% (best from 6m SL/ROI sweep)
PROD_STRATEGY_STOPLOSS = -0.15
PROD_STRATEGY_TAKE_PROFIT = 0.05
PROD_STRATEGY_MINIMAL_ROI = {"0": PROD_STRATEGY_TAKE_PROFIT}


class SimLiveSlTpMixin:
    """Prod: только процентный SL и TP 1:3, без exit_signal."""

    stoploss = PROD_STOPLOSS
    minimal_roi = PROD_MINIMAL_ROI
    use_exit_signal = False
    trailing_stop = False

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe


class SimLiveCooldownMixin:
    """MultiStrategyRouter calls pair_in_cooldown; sim _LiteBase uses _pair_in_cooldown."""

    def pair_in_cooldown(self, pair, current_time) -> bool:
        fn = getattr(self, "_pair_in_cooldown", None)
        if callable(fn):
            return bool(fn(pair, current_time))
        return False
