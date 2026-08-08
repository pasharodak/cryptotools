#!/usr/bin/env python3
"""Wire top-30 pack into prod configs/wrappers/router/UI/changelog."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
SCRIPTS = SITE / "scripts"
sys.path.insert(0, str(SCRIPTS))

MANIFEST = ROOT / "simulation/config/prod_top30_pack.json"
STRAT_DIR = SITE / "user_data/strategies"
ROUTER = STRAT_DIR / "MultiStrategyRouter.py"
PAIR_CFG = SCRIPTS / "pair_config_server.py"
APPLY = ROOT / "simulation/scripts/apply_prod_ml_config.py"
PROD_ML = ROOT / "simulation/config/prod_ml_bots.json"
AGENTS = ROOT / "AGENTS.md"
DEPLOY = SCRIPTS / "deploy_prod_ml.ps1"

MODULE_BY_GROUP = {
    "scalp": "simulation.strategies.SimScalpingStrategies",
    "newset": "simulation.strategies.SimNewSetStrategies",
    "chart": "simulation.strategies.SimChartTaStrategies",
    "chart2": "simulation.strategies.SimChartTaWave2Strategies",
    "chart3": "simulation.strategies.SimChartTaWave3Strategies",
    "combo": "simulation.strategies.SimComboStrategies",
}

EXISTING_WRAPPERS = {
    "DonchianBreakoutStrategy",
    "PsaraFlipStrategy",
    "KeltnerBreakoutStrategy",
    "ScalpMacdHistStrategy",
}

LEGACY_CLASSES = [
    "CriptoPairsStrategy",
    "SupertrendStrategy",
    "MacdEmaStrategy",
    "FibPullbackStrategy",
    "TripleEmaStrategy",
    "BollingerRsiStrategy",
    "AdxMomentumStrategy",
    "LiteIntradayStrategy",
    "LiteRangeStrategy",
]

LEGACY_SCEN = {
    "TripleEmaStrategy": {
        "scenario_id": "trend_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "TripleEmaStrategy",
        "label": "EMA 50/200 (4H)",
    },
    "BollingerRsiStrategy": {
        "scenario_id": "lite_mean_rev",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "BollingerRsiStrategy",
        "label": "Mean-reversion (BB)",
    },
    "AdxMomentumStrategy": {
        "scenario_id": "trend_breakout",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "AdxMomentumStrategy",
        "label": "Breakout-Retest",
    },
    "LiteIntradayStrategy": {
        "scenario_id": "lite_intraday",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteIntradayStrategy",
        "label": "Внутридневная",
    },
    "LiteRangeStrategy": {
        "scenario_id": "lite_range",
        "scan_type": "strategy",
        "group": "lite",
        "strategy": "LiteRangeStrategy",
        "label": "Диапазонная",
    },
    "SupertrendStrategy": {
        "scenario_id": "trend_supertrend",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "SupertrendStrategy",
        "label": "Supertrend (ATR) (ML Gate)",
    },
    "MacdEmaStrategy": {
        "scenario_id": "trend_macd_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "MacdEmaStrategy",
        "label": "MACD + EMA200 (ML Gate)",
    },
    "FibPullbackStrategy": {
        "scenario_id": "trend_fib",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "FibPullbackStrategy",
        "label": "Fib pullback (DCA) (ML Gate)",
    },
}


def write_wrapper(class_name: str, module: str, scenario_id: str) -> None:
    path = STRAT_DIR / f"{class_name}.py"
    if class_name in EXISTING_WRAPPERS and path.is_file():
        return
    path.write_text(
        dedent(
            f"""\
            # pragma pylint: disable=missing-docstring, invalid-name
            \"\"\"Prod alias for sim {scenario_id}.\"\"\"

            from _sim_live import SimLiveCooldownMixin
            from {module} import {class_name} as _Sim


            class {class_name}(SimLiveCooldownMixin, _Sim):
                # Keep sim SL/TP; MultiStrategyRouter applies per-tag risk.
                pass
            """
        ),
        encoding="utf-8",
    )
    print(f"wrapper {class_name}")


def _py_str_dict(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=4)


def rewrite_router(strategies: list[dict]) -> None:
    old = ROUTER.read_text(encoding="utf-8")
    body_start = old.index("\ndef load_enabled_map")
    body = old[body_start:]

    top_classes = [s["class_name"] for s in strategies]
    imports = sorted(set(LEGACY_CLASSES + top_classes))
    import_block = "\n".join(f"from {c} import {c}" for c in imports)

    reg_lines = [f'    "{c}": {c},' for c in LEGACY_CLASSES]
    for s in strategies:
        reg_lines.append(f'    "{s["class_name"]}": {s["class_name"]},')
    reg = "STRATEGY_REGISTRY: dict[str, type[IStrategy]] = {\n" + "\n".join(reg_lines) + "\n}"

    risk_lines = [
        f'    "{s["class_name"]}": {{"stoploss": {s["stoploss"]}, "tp": {s["tp"]}}},'
        for s in strategies
    ]
    risk = (
        "# Per-tag risk for ML pack (others keep router SL -15% / TP +5%).\n"
        "TAG_RISK: dict[str, dict[str, float]] = {\n"
        + "\n".join(risk_lines)
        + "\n}"
    )

    scen = dict(LEGACY_SCEN)
    for s in strategies:
        scen[s["class_name"]] = {
            "scenario_id": s["scenario_id"],
            "scan_type": "strategy",
            "group": s.get("group") or "ta",
            "strategy": s["class_name"],
            "label": s.get("label") or s["class_name"],
        }
    scen_lines = ["SCENARIO_BY_TAG: dict[str, dict[str, str]] = {"]
    for k, v in scen.items():
        scen_lines.append(f'    "{k}": {{')
        for kk, vv in v.items():
            scen_lines.append(f'        "{kk}": {json.dumps(vv, ensure_ascii=False)},')
        scen_lines.append("    },")
    scen_lines.append("}")
    scen_block = "\n".join(scen_lines)

    header = f'''# pragma pylint: disable=missing-docstring, invalid-name
"""Combines signals from enabled sub-strategies (see user_data/enabled_strategies.json)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pandas import DataFrame

import talib.abstract as ta
from ctengine.persistence import Trade
from ctengine.strategy import IStrategy

{import_block}

_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))
from ml.gate import allow_trade_entry, pop_entry_ml, save_ml_to_trade  # noqa: E402
from _sim_live import PROD_STRATEGY_MINIMAL_ROI, PROD_STRATEGY_STOPLOSS  # noqa: E402

from ctengine.strategy import stoploss_from_open

ENABLED_FILE = Path(__file__).resolve().parent.parent / "enabled_strategies.json"
DUAL_HEDGE_FILE = Path(__file__).resolve().parent.parent / "dual_hedge.json"
HEDGE_TAG_SUFFIX = ":hedge"
INV_TAG_SUFFIX = ":inv"

{reg}

{risk}

MEAN_REV_ADX_TAGS = frozenset({{"BollingerRsiStrategy", "LiteRangeStrategy", "CriptoPairsStrategy"}})

{scen_block}

'''
    ROUTER.write_text(header + body, encoding="utf-8")
    print("MultiStrategyRouter updated")


def update_pair_config(strategies: list[dict]) -> None:
    text = PAIR_CFG.read_text(encoding="utf-8")
    entries: list[str] = []
    # Pack first (by ui_order), then legacy — never drop catalog ids (history/stats labels).
    ordered = sorted(
        strategies,
        key=lambda s: (s.get("ui_order", s.get("rank", 999)), s.get("num", s.get("rank", 999))),
    )
    for s in ordered:
        num = int(s.get("num") if s.get("num") is not None else s["rank"])
        ui_order = int(s.get("ui_order") if s.get("ui_order") is not None else num)
        gate = s.get("min_profit_proba")
        gate_s = f"{int(round(float(gate) * 100))}%" if gate is not None else "-"
        desc = (
            f"ML pack · sim {s['scenario_id']} · "
            f"test ML PnL {float(s['score_ml_test_pnl']):.1f} USDT · "
            f"SL {s['stoploss']:.1%} · TP {s['tp']:.1%} · gate>={gate_s} · exp {s.get('winner_exp')}"
        )
        name = s["label"]  # num is separate; UI renders `#num name`
        entries.append(
            "    {\n"
            f'        "id": "{s["class_name"]}",\n'
            f'        "num": {num},\n'
            f'        "ui_order": {ui_order},\n'
            f'        "name": {json.dumps(name, ensure_ascii=False)},\n'
            f'        "desc": {json.dumps(desc, ensure_ascii=False)},\n'
            "    },"
        )
    for i, sid in enumerate(LEGACY_CLASSES):
        entries.append(
            "    {\n"
            f'        "id": "{sid}",\n'
            f'        "num": None,\n'
            f'        "ui_order": {1000 + i},\n'
            f'        "name": {json.dumps(sid.replace("Strategy", ""), ensure_ascii=False)},\n'
            f'        "desc": "Legacy · off by default. Kept in catalog for history/stats labels.",\n'
            "    },"
        )
    block = "AVAILABLE_STRATEGIES = [\n" + "\n".join(entries) + "\n]\n"
    i0 = text.index("AVAILABLE_STRATEGIES = [")
    i1 = text.index("\n\ndef normalize_pair")
    text = text[:i0] + block + text[i1:]

    prod_ids = ",\n    ".join(
        f'"{s["class_name"]}"'
        for s in sorted(
            strategies,
            key=lambda x: (x.get("ui_order", x.get("rank", 999)), x.get("num", x.get("rank", 999))),
        )
    )
    new_prod = "PROD_DEFAULT_STRATEGIES = frozenset({\n    " + prod_ids + ",\n})"
    j0 = text.index("PROD_DEFAULT_STRATEGIES = frozenset({")
    j1 = text.index("})", j0) + 2
    text = text[:j0] + new_prod + text[j1:]
    PAIR_CFG.write_text(text, encoding="utf-8")
    print("pair_config_server updated")


def update_apply_and_prod_ml(strategies: list[dict], legacy_disabled: list[str]) -> None:
    enabled = {c: False for c in legacy_disabled}
    sim_map = {
        "TripleEmaStrategy": "trend_ema",
        "AdxMomentumStrategy": "trend_breakout",
        "BollingerRsiStrategy": "lite_mean_rev",
        "LiteIntradayStrategy": "lite_intraday",
        "LiteRangeStrategy": "lite_range",
        "SupertrendStrategy": "trend_supertrend",
        "MacdEmaStrategy": "trend_macd_ema",
        "FibPullbackStrategy": "trend_fib",
    }
    for s in strategies:
        enabled[s["class_name"]] = True
        sim_map[s["class_name"]] = s["scenario_id"]
    inverted = {k: False for k in enabled}
    cfg = {
        "enabled": enabled,
        "inverted": inverted,
        "_note": (
            "Prod — top-30 by per-scenario ML param experiments (Apr cut). "
            "Ordered by ML test PnL. Legacy Jul set disabled."
        ),
        "_sim_map": sim_map,
        "_rank_order": [s["class_name"] for s in strategies],
    }

    lines = ["def _strategy_config() -> dict:", "    return {"]
    raw = json.dumps(cfg, indent=4, ensure_ascii=False)
    raw = raw.replace(": true", ": True").replace(": false", ": False")
    for line in raw.splitlines()[1:-1]:
        lines.append("    " + line)
    lines.append("    }")
    lines.append("")
    new_fn = "\n".join(lines) + "\n"

    apply_text = APPLY.read_text(encoding="utf-8")
    a0 = apply_text.index("def _strategy_config() -> dict:")
    a1 = apply_text.index("\ndef main() -> int:")
    apply_text = apply_text[:a0] + new_fn + apply_text[a1:]

    sync_snip = '''
    # Sync pack scenario models into site user_data
    by_src = MODEL_SRC / "by_scenario"
    by_dst = MODEL_DST / "by_scenario"
    by_dst.mkdir(parents=True, exist_ok=True)
    pack_path = ROOT / "simulation/config/prod_top30_pack.json"
    pack_sids: list[str] = []
    if pack_path.is_file():
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        pack_sids = [s["scenario_id"] for s in pack.get("strategies") or []]
    for sid in pack_sids + ["live_grid"]:
        src_dir = by_src / sid
        if not src_dir.is_dir():
            continue
        dst_dir = by_dst / sid
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in ("pnl_classifier.joblib", "pnl_classifier_meta.json"):
            src = src_dir / name
            if src.is_file():
                shutil.copy2(src, dst_dir / name)
        print(f"scenario model -> by_scenario/{sid}")

'''
    if "prod_top30_pack.json" not in apply_text or "Sync pack scenario models" not in apply_text:
        marker = '    (MODEL_DST / "deploy_meta.json").write_text'
        if marker in apply_text and "Sync pack scenario models" not in apply_text:
            apply_text = apply_text.replace(marker, sync_snip + marker, 1)

    # deploy_meta note
    apply_text = apply_text.replace(
        '"model": "xgboost + isotonic (pnl_classifier)",',
        '"model": "per-scenario winners from ml_param_experiments (top-30 pack)",',
    )
    APPLY.write_text(apply_text, encoding="utf-8")
    print("apply_prod_ml_config.py updated")

    prod = json.loads(PROD_ML.read_text(encoding="utf-8"))
    prod["updated"] = "2026-08-03"
    prod["enabled_scenarios"] = ["live_grid"] + [s["scenario_id"] for s in strategies]
    prod["prod_strategy_bots"] = [s["scenario_id"] for s in strategies]
    prod["ml_gate"]["strategy_bots"] = {
        "gate_mode": "profit_only",
        "min_confidence": 0.45,
        "note": "Floor; per-scenario min_profit_proba from model meta overrides",
    }
    prod["pack"] = "prod_top30_pack.json"
    PROD_ML.write_text(json.dumps(prod, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print("prod_ml_bots.json updated")


def update_deploy_ps1(strategies: list[dict]) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    sids = [s["scenario_id"] for s in strategies]
    classes = [s["class_name"] for s in strategies]
    wrappers = " ".join(f"user_data/strategies/{c}.py" for c in classes)
    # Replace strategies scp line (single-line expected)
    text2 = re.sub(
        r'& \$scp -i \$KeyPath user_data/strategies/TradeFinderStrategy\.py[^\n]+',
        '& $scp -i $KeyPath user_data/strategies/TradeFinderStrategy.py '
        "user_data/strategies/MultiStrategyRouter.py "
        "user_data/strategies/VolatilityGridStrategy.py "
        "user_data/strategies/_sim_live.py "
        + wrappers
        + ' "${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/strategies/"',
        text,
        count=1,
    )
    sid_list = ", ".join(f'"{s}"' for s in sids)
    text2 = re.sub(
        r"foreach \(\$sid in @\([^)]*\)\) \{",
        f"foreach ($sid in @({sid_list})) {{",
        text2,
        count=1,
    )
    if "prod_top30_pack.json" not in text2:
        text2 = text2.replace(
            '& $scp -i $KeyPath "$Sim\\config\\ml_training_pairs_whitelist.json" '
            '"${SshUser}@${ServerIp}:/home/cryptotools/app/simulation/config/"',
            '& $scp -i $KeyPath "$Sim\\config\\ml_training_pairs_whitelist.json" '
            '"$Sim\\config\\prod_top30_pack.json" "$Sim\\config\\prod_ml_bots.json" '
            '"${SshUser}@${ServerIp}:/home/cryptotools/app/simulation/config/"\n'
            "& $scp -i $KeyPath user_data/grid_changelog.json "
            '"${SshUser}@${ServerIp}:/home/cryptotools/app/user_data/"',
        )
    DEPLOY.write_text(text2, encoding="utf-8")
    print("deploy_prod_ml.ps1 updated")


def update_agents(strategies: list[dict]) -> None:
    text = AGENTS.read_text(encoding="utf-8")
    top_lines = "\n".join(
        f"- #{s['rank']} `{s['class_name']}` (`{s['scenario_id']}`) — ML PnL "
        f"{float(s['score_ml_test_pnl']):.1f}, exp `{s.get('winner_exp')}`, "
        f"gate {s.get('min_profit_proba')}"
        for s in strategies[:10]
    )
    new_sec = f"""## 6. Текущий prod enable set (ориентир)

Включены **top-30** из per-scenario ML param experiments (cut 2026-04-01), по убыванию ML test PnL.
Legacy Jul set (MacdEma/Fib/TripleEma/…) — **выключены**, wrappers оставлены.

Топ-10:
{top_lines}
- … ещё 20 — см. `simulation/config/prod_top30_pack.json`

ML gate (strategy bots): `profit_only`, floor `min_confidence` **0.45**; per-scenario порог из `pnl_classifier_meta.json` (`min_profit_proba`).
Архив прежних моделей: `archives/prod_models_*.zip`.

"""
    text2 = re.sub(
        r"## 6\. Текущий prod enable set \(ориентир\).*?## 7\.",
        new_sec + "## 7.",
        text,
        count=1,
        flags=re.S,
    )
    text2 = text2.replace(
        "Дата ориентира: **2026-08-02**.",
        "Дата ориентира: **2026-08-03**.",
    )
    # TAG_RISK blurb
    text2 = re.sub(
        r"### Per-tag risk \(`TAG_RISK` в MultiStrategyRouter\).*?### Паттерн prod wrapper",
        "### Per-tag risk (`TAG_RISK` в MultiStrategyRouter)\n\n"
        "Для всех top-30 из `prod_top30_pack.json` — SL/TP из `player_scenarios.json` "
        "(per-tag). Legacy без записи → глобальные −15% / +5%.\n\n"
        "### Паттерн prod wrapper",
        text2,
        count=1,
        flags=re.S,
    )
    AGENTS.write_text(text2, encoding="utf-8")
    print("AGENTS.md updated")


def changelog(strategies: list[dict]) -> None:
    from grid_changelog import append_note

    names = ", ".join(f"#{s['rank']} {s['class_name']}" for s in strategies[:8])
    append_note(
        text=(
            f"Деплой ML pack top-30: включены стратегии по ML test PnL ({names}, ...). "
            "Legacy Jul set выключен. Per-scenario модели + пороги gate из экспериментов. "
            "Старые модели: archives/prod_models_*.zip."
        ),
        category="strategy",
        source="deploy",
    )
    append_note(
        text="ML gate strategy floor: profit >=45% (per-scenario meta задаёт свой порог).",
        category="strategy",
        source="deploy",
    )
    print("changelog entries added")


def main() -> int:
    pack = json.loads(MANIFEST.read_text(encoding="utf-8"))
    strategies = pack["strategies"]
    legacy = pack.get("legacy_disabled") or LEGACY_CLASSES

    for s in strategies:
        mod = MODULE_BY_GROUP[s["group"]]
        write_wrapper(s["class_name"], mod, s["scenario_id"])

    rewrite_router(strategies)
    update_pair_config(strategies)
    update_apply_and_prod_ml(strategies, legacy)
    update_deploy_ps1(strategies)
    update_agents(strategies)
    changelog(strategies)
    print("OK — wire complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
