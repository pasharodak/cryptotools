#!/usr/bin/env python3
"""Apply prod ML config: filter bots, enable gate, sync model, update live strategy map."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
SCRIPTS = SITE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from grid_changelog import record_ml_gate_confidence  # noqa: E402

PROD_CFG = ROOT / "simulation/config/prod_ml_bots.json"
SCENARIOS = ROOT / "simulation/config/player_scenarios.json"
ML_GATE = ROOT / "simulation/config/ml_entry_gate.json"
LIVE_ML_GATE = SITE / "user_data/ml_entry_gate.json"
ENABLED_STRAT = SITE / "user_data/enabled_strategies.json"
BOT_STRAT = SITE / "user_data/bot_strategies.json"
MODEL_SRC = ROOT / "simulation/results/trade_db/models"
MODEL_DST = SITE / "user_data/models/pnl_classifier"
FINDER_SRC = MODEL_SRC
FINDER_DST = SITE / "user_data/models/trade_finder"
FINDER_CFG_SRC = ROOT / "simulation/config/trade_finder.json"
FINDER_CFG_DST = SITE / "user_data/trade_finder.json"


def _strategy_config() -> dict:
    """Build enabled_strategies payload from prod_top30_pack (+ legacy off)."""
    legacy = [
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
    pack_path = ROOT / "simulation/config/prod_top30_pack.json"
    strategies = []
    if pack_path.is_file():
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        strategies = pack.get("strategies") or []
        legacy = pack.get("legacy_disabled") or legacy
    enabled = {c: False for c in legacy}
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
    rank_order: list[str] = []
    trained_risk = {c: False for c in legacy}
    ml_confidence = {c: 0.55 for c in legacy}
    for s in strategies:
        cls = s["class_name"]
        enabled[cls] = True
        trained_risk[cls] = True
        sim_map[cls] = s["scenario_id"]
        rank_order.append(cls)
        try:
            ml_confidence[cls] = float(s.get("min_profit_proba") or 0.55)
        except (TypeError, ValueError):
            ml_confidence[cls] = 0.55
    return {
        "enabled": enabled,
        "inverted": {k: False for k in enabled},
        "trained_risk": trained_risk,
        "ml_confidence": ml_confidence,
        "_note": (
            "Prod — top-30 by per-scenario ML param experiments (Apr cut). "
            "Ordered by ML test PnL. Legacy Jul set disabled. "
            "trained_risk=true → SL/TP from sim scenarios. "
            "ml_confidence → per-strategy ML gate min profit confidence."
        ),
        "_sim_map": sim_map,
        "_rank_order": rank_order,
    }


def main() -> int:
    prod = json.loads(PROD_CFG.read_text(encoding="utf-8"))
    enabled_set = set(prod["enabled_scenarios"])

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    for sc in scenarios:
        sid = sc["id"]
        if sid in enabled_set:
            sc["enabled"] = True
            sc.pop("prod_disabled_reason", None)
        else:
            sc["enabled"] = False
            reason = prod.get("disabled_scenarios", {}).get(sid)
            if reason:
                sc["prod_disabled_reason"] = reason
    SCENARIOS.write_text(json.dumps(scenarios, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"player_scenarios.json — enabled {len(enabled_set)} bots")

    gate = prod.get("ml_gate") or {}
    gate["enabled"] = True
    old_gate: dict = {}
    if LIVE_ML_GATE.is_file():
        try:
            old_gate = json.loads(LIVE_ML_GATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old_gate = {}
    old_conf = float((old_gate.get("strategy_bots") or {}).get("min_confidence") or 0.8)
    new_conf = float((gate.get("strategy_bots") or {}).get("min_confidence") or 0.8)
    ML_GATE.write_text(json.dumps(gate, indent=4) + "\n", encoding="utf-8")
    LIVE_ML_GATE.write_text(json.dumps(gate, indent=4) + "\n", encoding="utf-8")
    if abs(old_conf - new_conf) >= 1e-9:
        record_ml_gate_confidence(old_conf, new_conf, source="deploy")
        print(f"changelog — ML gate profit {int(old_conf * 100)}% в†’ {int(new_conf * 100)}%")
    print(f"ml_entry_gate.json — finder={gate.get('gate_mode', 'block_loss')}, grid_bots={gate.get('grid_bots', {})}, strategy_bots={gate.get('strategy_bots', {})} (sim + live)")

    MODEL_DST.mkdir(parents=True, exist_ok=True)
    for name in ("pnl_classifier.joblib", "pnl_classifier_meta.json"):
        src = MODEL_SRC / name
        if src.is_file():
            shutil.copy2(src, MODEL_DST / name)
            print(f"model -> user_data/models/pnl_classifier/{name}")

    deploy_meta = {
        "deployed_at": datetime.now(tz=UTC).isoformat(),
        "model": "per-scenario winners from ml_param_experiments (top-30 pack)",
        "enabled_scenarios": prod["enabled_scenarios"],
        "ml_gate": gate,
    }

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

    (MODEL_DST / "deploy_meta.json").write_text(json.dumps(deploy_meta, indent=2), encoding="utf-8")

    FINDER_DST.mkdir(parents=True, exist_ok=True)
    for name in ("trade_finder.joblib", "trade_finder_meta.json"):
        src = FINDER_SRC / name
        if src.is_file():
            shutil.copy2(src, FINDER_DST / name)
            print(f"model -> user_data/models/trade_finder/{name}")
    if FINDER_CFG_SRC.is_file():
        shutil.copy2(FINDER_CFG_SRC, FINDER_CFG_DST)
        print("trade_finder.json synced from simulation/config")

    cfg_path = SITE / "user_data/config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        changed = False
        if cfg.get("strategy") != "TradeFinderStrategy":
            cfg["strategy"] = "TradeFinderStrategy"
            cfg["bot_name"] = "criptotools-finder"
            cfg.pop("mltrain_model", None)
            cfg.pop("mltrain", None)  # obsolete upstream config block
            cfg["db_url"] = "sqlite:///tradesv3-finder.sqlite"
            cfg["_prod_note"] = "ML Trade Finder (XGBoost scanner + pnl classifier gate) — replaces legacy ML bot"
            changed = True
        if changed:
            cfg_path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
            print("config.json — TradeFinderStrategy (ML Finder bot)")

    strat = _strategy_config()
    ENABLED_STRAT.write_text(json.dumps(strat, indent=4) + "\n", encoding="utf-8")
    BOT_STRAT.write_text(
        json.dumps(
            {
                "enabled": strat["enabled"],
                "inverted": strat.get("inverted") or {},
                "trained_risk": strat.get("trained_risk") or {},
                "ml_confidence": strat.get("ml_confidence") or {},
            },
            indent=4,
        )
        + "\n",
        encoding="utf-8",
    )
    print("enabled_strategies.json — ML pack from prod_top30_pack (legacy off)")

    print("\nProd bots enabled:")
    for sid in prod["enabled_scenarios"]:
        label = next((s["label"] for s in scenarios if s["id"] == sid), sid)
        line = f"  + {label} ({sid})"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))
    print("\nVPS: enable Finder unit, keep grid + strategy units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
