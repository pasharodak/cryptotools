# pragma pylint: disable=missing-docstring, invalid-name
"""MultiStrategyRouter — baseline before optimization (Jun 25)."""

from __future__ import annotations

import json
from pathlib import Path

from pandas import DataFrame

from MultiStrategyRouter import STRATEGY_REGISTRY, MultiStrategyRouter

ENABLED_FILE = Path(__file__).resolve().parent.parent / "enabled_strategies_baseline.json"


def load_enabled_map() -> dict[str, bool]:
    if not ENABLED_FILE.is_file():
        return {"CriptoPairsStrategy": True, "SupertrendStrategy": True}
    data = json.loads(ENABLED_FILE.read_text(encoding="utf-8"))
    enabled = data.get("enabled", {})
    return {sid: bool(enabled.get(sid, False)) for sid in STRATEGY_REGISTRY}


class MultiStrategyRouterBaseline(MultiStrategyRouter):
    minimal_roi = {
        "0": 0.03,
        "60": 0.015,
        "120": 0.005,
        "240": 0,
    }
    use_exit_signal = True
    adx_max_entry = 999

    def _enabled_ids(self) -> list[str]:
        enabled = load_enabled_map()
        return [sid for sid, on in enabled.items() if on]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        for sid in self._enabled_ids():
            strat = self._get_instance(sid)
            df = strat.populate_indicators(dataframe.copy(), metadata)
            df = strat.populate_entry_trend(df, metadata)

            long_mask = df.get("enter_long", 0).fillna(0).astype(int) == 1
            short_mask = df.get("enter_short", 0).fillna(0).astype(int) == 1

            first_long = long_mask & (dataframe["enter_long"] != 1)
            first_short = short_mask & (dataframe["enter_short"] != 1)
            dataframe.loc[first_long, "enter_tag"] = sid
            dataframe.loc[first_short, "enter_tag"] = sid
            dataframe.loc[long_mask, "enter_long"] = 1
            dataframe.loc[short_mask, "enter_short"] = 1

        return dataframe
