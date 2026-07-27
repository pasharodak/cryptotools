"""Combine trade finder signals with pnl_classifier entry gate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simulation.ml.trade_gate import MlEntryGate


def load_gate_scenario(root: Path, scenario_id: str) -> dict[str, Any]:
    path = root / "simulation/config/player_scenarios.json"
    scenarios = json.loads(path.read_text(encoding="utf-8"))
    for sc in scenarios:
        if sc.get("id") == scenario_id and sc.get("enabled", True):
            return sc
    raise ValueError(f"scenario not found or disabled: {scenario_id}")


def gate_inst_config(cfg: dict[str, Any], atr_pct: float) -> dict[str, Any]:
    """Map finder ATR barriers to classifier stoploss/roi features."""
    sl_mult = float(cfg.get("sl_atr_mult") or 0.8)
    tp_mult = float(cfg.get("tp_atr_mult") or 2.0)
    ap = max(float(atr_pct or 0), 0.001)
    return {
        "stake": float(cfg.get("stake_usdt") or 10),
        "stoploss": round(-sl_mult * ap, 6),
        "minimal_roi": {"0": round(tp_mult * ap, 6)},
        "timeframe": cfg.get("timeframe", "5m"),
    }


def make_gate(root: Path, gate_cfg: dict[str, Any]) -> MlEntryGate:
    gate = MlEntryGate(root)
    gate.config["enabled"] = True
    gate.config["gate_mode"] = gate_cfg.get("gate_mode", "block_loss")
    gate.config["block_predicted"] = gate_cfg.get("block_predicted", "loss")
    gate.config["min_confidence"] = float(gate_cfg.get("min_confidence") or 0.0)
    return gate


def classify_signal(
    signal: dict[str, Any],
    *,
    gate: MlEntryGate,
    scenario: dict[str, Any],
    finder_cfg: dict[str, Any],
    atr_pct: float,
    armed_offset_ms: int,
) -> tuple[bool, dict[str, Any]]:
    """Return (keep, signal with gate_ml). keep=False if classifier blocks."""
    inst = gate_inst_config(finder_cfg, atr_pct)
    trade = {
        "open_ms": signal["open_ms"],
        "open_rate": signal["open_rate"],
        "is_short": signal["is_short"],
    }
    armed_at = int(signal["open_ms"]) - armed_offset_ms
    ml = gate.predict_for_trade(scenario, signal["pair"], trade, inst, armed_at)
    enriched = {**signal, "gate_ml": ml, "gate_inst": inst}
    return not gate.should_block(ml), enriched
