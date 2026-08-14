#!/usr/bin/env python3
"""Merge pack catalog into enabled_strategies without changing existing toggles.

Used on VPS during deploy so UI enable/ml_confidence/trained_risk survive.
New pack class_names are added as enabled=False.
legacy_disabled are forced off.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

USER_DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/cryptotools/app/user_data")
PACK = Path(sys.argv[2] if len(sys.argv) > 2 else "/home/cryptotools/app/simulation/config/prod_top30_pack.json")
ENABLED = USER_DATA / "enabled_strategies.json"
BOT = USER_DATA / "bot_strategies.json"


def main() -> int:
    if not ENABLED.is_file():
        print("no enabled_strategies.json — skip")
        return 0
    if not PACK.is_file():
        print("no pack — skip")
        return 0

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    bak = USER_DATA / f"enabled_strategies.bak.{stamp}.json"
    shutil.copy2(ENABLED, bak)
    print(f"backup -> {bak.name}")

    data = json.loads(ENABLED.read_text(encoding="utf-8"))
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    strategies = pack.get("strategies") or []
    legacy = {str(x) for x in (pack.get("legacy_disabled") or [])}

    enabled = dict(data.get("enabled") or {})
    inverted = dict(data.get("inverted") or {})
    trained = dict(data.get("trained_risk") or {})
    mlc = dict(data.get("ml_confidence") or {})
    sim_map = dict(data.get("_sim_map") or {})
    rank_order = [s["class_name"] for s in strategies if s.get("class_name")]

    added = []
    for s in strategies:
        cls = s.get("class_name")
        if not cls:
            continue
        sim_map[cls] = s.get("scenario_id") or sim_map.get(cls)
        try:
            gate = float(s.get("min_profit_proba") or 0.55)
        except (TypeError, ValueError):
            gate = 0.55
        if cls not in enabled:
            enabled[cls] = False
            trained[cls] = True
            mlc[cls] = gate
            inverted[cls] = False
            added.append(cls)
        else:
            trained.setdefault(cls, True)
            mlc.setdefault(cls, gate)
            inverted.setdefault(cls, False)

    for cls in legacy:
        enabled[cls] = False
        trained.setdefault(cls, False)
        mlc.setdefault(cls, 0.55)
        inverted.setdefault(cls, False)

    out = {
        **{k: v for k, v in data.items() if k.startswith("_") or k not in {
            "enabled", "inverted", "trained_risk", "ml_confidence", "_sim_map", "_rank_order"
        }},
        "enabled": enabled,
        "inverted": {k: bool(inverted.get(k, False)) for k in enabled},
        "trained_risk": {k: bool(trained.get(k, False)) for k in enabled},
        "ml_confidence": {k: float(mlc.get(k, 0.55)) for k in enabled},
        "_sim_map": sim_map,
        "_rank_order": rank_order,
        "_note": data.get("_note")
        or "UI toggles preserved on deploy; new pack ids default OFF.",
    }
    ENABLED.write_text(json.dumps(out, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    bot = {
        "enabled": out["enabled"],
        "inverted": out["inverted"],
        "trained_risk": out["trained_risk"],
        "ml_confidence": out["ml_confidence"],
    }
    BOT.write_text(json.dumps(bot, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    on = sum(1 for v in enabled.values() if v)
    print(f"merged on={on} added_new={added or []}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
