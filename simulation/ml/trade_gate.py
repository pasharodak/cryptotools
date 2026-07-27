"""ML entry gate — block trades when model predicts loss."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simulation.ml.pnl_classifier import load_model, make_market_store, predict_entry
from simulation.ml.per_strategy_models import load_model_for_scenario

DEFAULT_CONFIG = {
    "enabled": True,
    "gate_mode": "block_loss",
    "block_predicted": "loss",
    "min_confidence": 0.0,
    "grid_bots": {
        "gate_mode": "profit_only",
        "min_confidence": 0.8,
    },
    "strategy_bots": {
        "gate_mode": "profit_only",
        "min_confidence": 0.8,
    },
}

GRID_SCENARIO_ID = "live_grid"


def _resolve_gate_rules(config: dict[str, Any], scenario: dict[str, Any] | None) -> tuple[str, float]:
    sid = (scenario or {}).get("id") or (scenario or {}).get("scenario_id") or ""
    if sid == GRID_SCENARIO_ID:
        grid = config.get("grid_bots") or {}
        if grid:
            return (
                grid.get("gate_mode", "profit_only"),
                float(grid.get("min_confidence") or 0.8),
            )
    strat = config.get("strategy_bots") or {}
    if strat and sid != GRID_SCENARIO_ID:
        return (
            strat.get("gate_mode", "profit_only"),
            float(strat.get("min_confidence") or 0.8),
        )
    return (
        config.get("gate_mode", "block_loss"),
        float(config.get("min_confidence") or 0.0),
    )


def load_gate_config(root: Path) -> dict[str, Any]:
    path = root / "simulation/config/ml_entry_gate.json"
    if not path.is_file():
        return dict(DEFAULT_CONFIG)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {**DEFAULT_CONFIG, **cfg}


def save_gate_config(root: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    path = root / "simulation/config/ml_entry_gate.json"
    merged = {**DEFAULT_CONFIG, **cfg}
    path.write_text(json.dumps(merged, indent=4) + "\n", encoding="utf-8")
    return merged


class MlEntryGate:
    def __init__(self, root: Path, *, model_mode: str = "global"):
        self.root = root
        self.config = load_gate_config(root)
        self.model_mode = model_mode  # global | per_strategy
        self._pipe = None
        self._pipes: dict[str, Any] = {}
        self._market = None

    def set_model_mode(self, mode: str) -> None:
        self.model_mode = mode
        self._pipe = None
        self._pipes = {}

    def _get_pipe(self, scenario_id: str):
        if self.model_mode == "per_strategy":
            if scenario_id not in self._pipes:
                self._pipes[scenario_id] = load_model_for_scenario(self.root, scenario_id)
            return self._pipes[scenario_id]
        self._ensure_model()
        return self._pipe

    def _model_ready(self) -> bool:
        if self.model_mode == "per_strategy":
            base = self.root / "simulation/results/trade_db/models/pnl_classifier.joblib"
            if base.is_file():
                return True
            by_dir = self.root / "simulation/results/trade_db/models/by_scenario"
            return by_dir.is_dir() and any(by_dir.iterdir())
        try:
            load_model(self.root)
            return True
        except FileNotFoundError:
            return False

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    def set_enabled(self, on: bool) -> None:
        self.config["enabled"] = bool(on)
        save_gate_config(self.root, self.config)

    def status(self) -> dict[str, Any]:
        ready = self._model_ready()
        model_name = None
        try:
            meta_path = self.root / "simulation/results/trade_db/models/pnl_classifier_meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                model_name = meta.get("model") or meta.get("model_key")
        except Exception:
            pass
        return {
            "enabled": self.enabled,
            "ready": ready,
            "model": model_name,
            "model_mode": self.model_mode,
            "gate_mode": self.config.get("gate_mode", "block_loss"),
            "block_predicted": self.config.get("block_predicted", "loss"),
            "min_confidence": self.config.get("min_confidence", 0.0),
        }

    def _ensure_model(self) -> None:
        if self._pipe is None:
            self._pipe = load_model(self.root)
            self._market = make_market_store(self.root)

    def predict_for_trade(
        self,
        sc: dict[str, Any],
        pair: str,
        trade: dict[str, Any],
        inst_config: dict[str, Any],
        armed_at_ms: int | None,
    ) -> dict[str, Any]:
        if self._market is None:
            self._market = make_market_store(self.root)
        sid = sc.get("id") or sc.get("scenario_id") or ""
        pipe = self._get_pipe(sid)
        body = {
            "scenario_id": sc.get("id"),
            "pair": pair,
            "label": sc.get("label"),
            "open_ms": trade.get("open_ms"),
            "open_rate": trade.get("open_rate"),
            "is_short": trade.get("is_short"),
            "basis": {
                "scenario_id": sc.get("id"),
                "pair": pair,
                "scan_type": sc.get("scan_type"),
                "group": sc.get("group"),
                "strategy": sc.get("strategy"),
                "stake_usdt": inst_config.get("stake"),
                "stoploss": inst_config.get("stoploss"),
                "minimal_roi": inst_config.get("minimal_roi"),
                "timeframe": inst_config.get("timeframe", "5m"),
                "armed_at_ms": armed_at_ms,
            },
            "trade": trade,
        }
        return predict_entry(pipe, body, self._market)

    def should_block(self, ml: dict[str, Any], scenario: dict[str, Any] | None = None) -> bool:
        mode, min_conf = _resolve_gate_rules(self.config, scenario)
        predicted = ml.get("predicted")
        conf = float(ml.get("confidence") or 0.0)
        if mode == "profit_only":
            if predicted != "profit":
                return True
            profit_conf = float(ml.get("confidence_profit") or (conf if predicted == "profit" else 0.0))
            return profit_conf < min_conf
        block = self.config.get("block_predicted", "loss")
        if predicted != block:
            return False
        return conf >= min_conf

    def evaluate_trades(
        self,
        trades: list[dict],
        sc: dict[str, Any],
        pair: str,
        inst_config: dict[str, Any],
        armed_at_ms: int | None,
    ) -> tuple[list[dict], list[dict], dict[str, Any]]:
        """Score all trades; split into would-enter vs would-block (loss)."""
        st = self.status()
        if not st.get("ready"):
            return list(trades), [], {"ready": False, "kept": len(trades), "skipped": 0}
        kept: list[dict] = []
        skipped: list[dict] = []
        for tr in trades:
            ml = self.predict_for_trade(sc, pair, tr, inst_config, armed_at_ms)
            row = {**tr, "ml": ml}
            if self.should_block(ml, scenario=sc):
                skipped.append(row)
            else:
                kept.append(row)
        return kept, skipped, {
            "ready": True,
            "kept": len(kept),
            "skipped": len(skipped),
            "gate_mode": self.config.get("gate_mode", "block_loss"),
            "block_predicted": self.config.get("block_predicted", "loss"),
        }

    def filter_trades(
        self,
        trades: list[dict],
        sc: dict[str, Any],
        pair: str,
        inst_config: dict[str, Any],
        armed_at_ms: int | None,
    ) -> tuple[list[dict], list[dict], dict[str, Any]]:
        kept, skipped, base = self.evaluate_trades(trades, sc, pair, inst_config, armed_at_ms)
        if not base.get("ready"):
            return list(trades), [], {**base, "enabled": self.enabled}
        if not self.enabled:
            annotated = kept + skipped
            annotated.sort(key=lambda t: t["open_ms"])
            return annotated, [], {**base, "enabled": False, "kept": len(trades), "skipped": 0}
        return kept, skipped, {**base, "enabled": True}
