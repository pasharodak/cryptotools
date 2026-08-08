#!/usr/bin/env python3
"""Sync TAG_RISK SL + full minimal_roi schedules from player_scenarios (test parity)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "simulation/config/prod_top30_pack.json"
SCEN = ROOT / "simulation/config/player_scenarios.json"
ROUTER = ROOT / "site/user_data/strategies/MultiStrategyRouter.py"


def main() -> int:
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in json.loads(SCEN.read_text(encoding="utf-8"))}

    # Enrich pack with full roi
    for s in pack["strategies"]:
        sc = by_id[s["scenario_id"]]
        roi = sc.get("minimal_roi") or {"0": s["tp"]}
        # normalize keys to str
        roi = {str(k): float(v) for k, v in roi.items()}
        s["stoploss"] = float(sc["stoploss"])
        s["tp"] = float(roi.get("0", s["tp"]))
        s["minimal_roi"] = roi
    PACK.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    risk_lines = []
    for s in pack["strategies"]:
        roi_repr = json.dumps(s["minimal_roi"], ensure_ascii=False)
        risk_lines.append(
            f'    "{s["class_name"]}": {{'
            f'"stoploss": {s["stoploss"]}, '
            f'"tp": {s["tp"]}, '
            f'"minimal_roi": {roi_repr}'
            f"}},"
        )
    risk_block = (
        "# Per-tag risk for ML pack — SL/ROI as in player_scenarios (sim test).\n"
        "TAG_RISK: dict[str, dict] = {\n"
        + "\n".join(risk_lines)
        + "\n}"
    )

    text = ROUTER.read_text(encoding="utf-8")
    text2 = re.sub(
        r"# Per-tag risk.*?\nTAG_RISK: dict\[str, dict(?:\[str, float\])?\] = \{.*?\n\}",
        risk_block,
        text,
        count=1,
        flags=re.S,
    )
    if text2 == text:
        raise SystemExit("TAG_RISK block not found/replaced")

    # Replace custom_roi to honor stepped schedule
    old_roi = '''    def custom_roi(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        trade_duration: int,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | None:
        tag = base_enter_tag(entry_tag or trade.enter_tag)
        risk = TAG_RISK.get(tag)
        if not risk:
            return None
        return float(risk["tp"])
'''
    new_roi = '''    def custom_roi(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        trade_duration: int,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float | None:
        tag = base_enter_tag(entry_tag or trade.enter_tag)
        risk = TAG_RISK.get(tag)
        if not risk:
            return None
        roi_map = risk.get("minimal_roi")
        if isinstance(roi_map, dict) and roi_map:
            # Same rule as ctengine/freqtrade: largest key <= trade_duration (minutes).
            keys = sorted((int(k), float(v)) for k, v in roi_map.items())
            chosen = float(keys[0][1])
            for mins, val in keys:
                if trade_duration >= mins:
                    chosen = val
            return chosen
        return float(risk["tp"])
'''
    if old_roi not in text2:
        # try flexible match
        text2 = re.sub(
            r"    def custom_roi\(.*?return float\(risk\[\"tp\"\]\)\n",
            new_roi,
            text2,
            count=1,
            flags=re.S,
        )
    else:
        text2 = text2.replace(old_roi, new_roi)

    # confirm_trade_entry already uses risk tp for roi_at_entry — keep using "0"
    ROUTER.write_text(text2, encoding="utf-8")
    print(f"Updated TAG_RISK for {len(pack['strategies'])} strategies + stepped custom_roi")
    for s in pack["strategies"][:3]:
        print(f"  {s['class_name']}: SL={s['stoploss']} ROI={s['minimal_roi']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
