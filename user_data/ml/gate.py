"""ML entry gate for live CryptoTools — block entries predicted as loss."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from ml.joblib_compat import register_simulation_stubs
from ml.market_features import MARKET_FEATURES, features_from_ohlcv

logger = logging.getLogger(__name__)

USER_DATA = Path(__file__).resolve().parent.parent
CONFIG_PATH = USER_DATA / "ml_entry_gate.json"
MODEL_PATH = USER_DATA / "models/pnl_classifier/pnl_classifier.joblib"
META_PATH = USER_DATA / "models/pnl_classifier/pnl_classifier_meta.json"

CAT_FEATURES = [
    "scenario_id",
    "pair_base",
    "scan_type",
    "group",
    "strategy",
    "is_short",
]
NUM_FEATURES = [
    "stake_usdt",
    "stoploss",
    "roi_at_entry",
    "hour_utc",
    "dow_utc",
    "mins_since_armed",
    "log_open_rate",
] + MARKET_FEATURES

DEFAULT_CONFIG = {
    "enabled": True,
    "gate_mode": "block_loss",
    "block_predicted": "loss",
    "min_confidence": 0.0,
    "grid_bots": {
        "gate_mode": "profit_only",
        "min_confidence": 0.7,
    },
    "finder_bots": {
        "gate_mode": "profit_only",
        "min_confidence": 0.6,
    },
    "strategy_bots": {
        "gate_mode": "profit_only",
        "min_confidence": 0.6,
    },
}

GRID_SCENARIO_ID = "live_grid"
FINDER_STRATEGY = "TradeFinderStrategy"
GRID_MODEL_REL = "models/pnl_classifier/by_scenario/live_grid/pnl_classifier.joblib"

# UI / API choices for per-strategy ML profit confidence.
ML_CONFIDENCE_CHOICES: tuple[float, ...] = (
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
)

_GATE: LiveMlGate | None = None
_PENDING_ML: dict[str, dict[str, Any]] = {}
_ML_CONF_CACHE: dict[str, Any] = {"mtime": None, "map": {}}


def load_ml_confidence_overrides(user_data: Path | None = None) -> dict[str, float]:
    """Per-strategy ML min confidence from enabled_strategies.json (class_name → float)."""
    path = (user_data or USER_DATA) / "enabled_strategies.json"
    if not path.is_file():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _ML_CONF_CACHE.get("path") == str(path) and _ML_CONF_CACHE.get("mtime") == mtime:
        return dict(_ML_CONF_CACHE.get("map") or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    raw = data.get("ml_confidence") or {}
    out: dict[str, float] = {}
    for sid, val in raw.items():
        try:
            out[str(sid)] = float(val)
        except (TypeError, ValueError):
            continue
    _ML_CONF_CACHE["path"] = str(path)
    _ML_CONF_CACHE["mtime"] = mtime
    _ML_CONF_CACHE["map"] = out
    return dict(out)


def pending_ml_key(pair: str, side: str, scenario_id: str) -> str:
    return f"{pair}|{side}|{scenario_id}"


def stash_entry_ml(pair: str, side: str, scenario: dict[str, Any], ml: dict[str, Any]) -> None:
    sid = (scenario or {}).get("scenario_id") or ""
    # Must include ready=True: persist_entry_ml treats missing ready as "no stash"
    # and would re-predict at fill with different features (wrong UI %).
    _PENDING_ML[pending_ml_key(pair, side, sid)] = {
        "predicted": ml.get("predicted"),
        "confidence_profit": ml.get("confidence_profit"),
        "confidence": ml.get("confidence"),
        "confidence_loss": ml.get("confidence_loss"),
        "ready": True,
        "source": "confirm",
    }


def pop_entry_ml(pair: str, side: str, scenario_id: str) -> dict[str, Any] | None:
    return _PENDING_ML.pop(pending_ml_key(pair, side, scenario_id), None)


def save_ml_to_trade(
    trade: Any,
    ml: dict[str, Any] | None,
    *,
    gate_ml: dict[str, Any] | None = None,
) -> None:
    if not ml and not gate_ml:
        return
    primary = ml or gate_ml
    conf = primary.get("confidence_profit")
    if conf is not None:
        trade.set_custom_data("ml_confidence", float(conf))
    pred = primary.get("predicted")
    if pred:
        trade.set_custom_data("ml_predicted", str(pred))
    if gate_ml and ml and gate_ml is not ml:
        gate_conf = gate_ml.get("confidence_profit")
        if gate_conf is not None:
            trade.set_custom_data("ml_gate_confidence", float(gate_conf))


def pair_base(pair: str) -> str:
    return (pair or "").split("/")[0]


def feature_columns() -> list[str]:
    return CAT_FEATURES + NUM_FEATURES


class LiveMlGate:
    def __init__(self, user_data: Path | None = None):
        self.user_data = user_data or USER_DATA
        self.config_path = self.user_data / "ml_entry_gate.json"
        self.model_path = self.user_data / "models/pnl_classifier/pnl_classifier.joblib"
        self.grid_model_path = self.user_data / GRID_MODEL_REL
        self.config = self._load_config()
        self._config_mtime: float | None = self._config_path_mtime()
        self._pipe = None
        self._grid_pipe = None
        self._scenario_pipes: dict[str, Any] = {}
        self._scenario_meta: dict[str, dict[str, Any]] = {}
        self._ready = False
        self._grid_ready = False
        self._armed_at_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    def _config_path_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime if self.config_path.is_file() else None
        except OSError:
            return None

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return dict(DEFAULT_CONFIG)
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        return {**DEFAULT_CONFIG, **cfg}

    def _refresh_config(self) -> None:
        """Hot-reload ml_entry_gate.json when the file changes (no bot restart)."""
        mtime = self._config_path_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        try:
            self.config = self._load_config()
            self._config_mtime = mtime
            logger.info(
                "ML gate config reloaded (grid=%s)",
                self.config.get("grid_bots") or {},
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ML gate config reload failed: %s", exc)

    @property
    def enabled(self) -> bool:
        self._refresh_config()
        return bool(self.config.get("enabled"))

    def _ensure_grid_model(self) -> bool:
        if self._grid_pipe is not None:
            return self._grid_ready
        if not self.grid_model_path.is_file():
            logger.debug("ML gate: no grid-specific model at %s — using global", self.grid_model_path)
            return False
        try:
            register_simulation_stubs()
            self._grid_pipe = joblib.load(self.grid_model_path)
            self._grid_ready = True
        except Exception as exc:
            logger.warning("ML gate: failed to load grid model: %s", exc)
            self._grid_ready = False
        return self._grid_ready

    def _scenario_model_path(self, scenario_id: str) -> Path:
        return self.user_data / "models/pnl_classifier/by_scenario" / scenario_id / "pnl_classifier.joblib"

    def _load_scenario_meta(self, scenario_id: str) -> dict[str, Any]:
        if scenario_id in self._scenario_meta:
            return self._scenario_meta[scenario_id]
        meta_path = self._scenario_model_path(scenario_id).parent / "pnl_classifier_meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        self._scenario_meta[scenario_id] = meta
        return meta

    def _ensure_scenario_model(self, scenario_id: str) -> bool:
        if not scenario_id:
            return False
        if scenario_id in self._scenario_pipes:
            return self._scenario_pipes[scenario_id] is not None
        path = self._scenario_model_path(scenario_id)
        if not path.is_file():
            self._scenario_pipes[scenario_id] = None
            return False
        try:
            register_simulation_stubs()
            self._scenario_pipes[scenario_id] = joblib.load(path)
            self._load_scenario_meta(scenario_id)
            return True
        except Exception as exc:
            logger.warning("ML gate: failed to load scenario model %s: %s", scenario_id, exc)
            self._scenario_pipes[scenario_id] = None
            return False

    def _ensure_model(self) -> bool:
        if self._pipe is not None:
            return self._ready
        if not self.model_path.is_file():
            logger.warning("ML gate: model not found at %s — entries allowed", self.model_path)
            return False
        try:
            register_simulation_stubs()
            self._pipe = joblib.load(self.model_path)
            self._ready = True
        except Exception as exc:
            logger.warning("ML gate: failed to load model: %s", exc)
            self._ready = False
        return self._ready

    def _build_row(
        self,
        *,
        scenario: dict[str, Any],
        pair: str,
        rate: float,
        is_short: bool,
        current_time: datetime,
        stake_usdt: float,
        stoploss: float,
        minimal_roi: dict,
        timeframe: str,
        market: dict[str, float],
    ) -> dict[str, Any]:
        dt = current_time if current_time.tzinfo else current_time.replace(tzinfo=UTC)
        open_ms = int(dt.timestamp() * 1000)
        roi0 = float(minimal_roi.get("0") or minimal_roi.get(0) or 0)
        row: dict[str, Any] = {
            "scenario_id": scenario["scenario_id"],
            "pair_base": pair_base(pair),
            "scan_type": scenario.get("scan_type", "strategy"),
            "group": scenario.get("group", "unknown"),
            "strategy": scenario.get("strategy", "unknown"),
            "is_short": "1" if is_short else "0",
            "stake_usdt": float(stake_usdt),
            "stoploss": float(stoploss),
            "roi_at_entry": roi0,
            "hour_utc": dt.astimezone(UTC).hour,
            "dow_utc": dt.astimezone(UTC).weekday(),
            "mins_since_armed": max(0.0, (open_ms - self._armed_at_ms) / 60000.0),
            "log_open_rate": float(np.log1p(rate)) if rate > 0 else 0.0,
        }
        for col, val in market.items():
            row[col] = val
        return row

    def _grid_model_usable(self) -> bool:
        if not self._ensure_grid_model():
            return False
        meta_path = self.grid_model_path.parent / "pnl_classifier_meta.json"
        if not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        n = int(meta.get("n_trades") or 0)
        pr = float(meta.get("profit_recall") or 0)
        roc = float(meta.get("roc_auc") or 0)
        # Small grid dataset: allow strong ROC + enough samples even if profit_recall is modest
        if n >= 120 and roc >= 0.68 and pr >= 0.10:
            return True
        return n >= 80 and pr >= 0.25 and roc >= 0.55

    def predict_row(self, row: dict[str, Any], *, scenario_id: str | None = None) -> dict[str, Any]:
        sid = scenario_id or row.get("scenario_id") or ""
        pipe = None
        scope = "global"
        if sid == GRID_SCENARIO_ID and self._grid_model_usable():
            pipe = self._grid_pipe
            scope = "grid"
        elif sid and self._ensure_scenario_model(sid):
            pipe = self._scenario_pipes[sid]
            scope = f"scenario:{sid}"
        elif self._ensure_model():
            pipe = self._pipe
        if pipe is None:
            return {"predicted": "profit", "confidence": 0.0, "ready": False}
        features = self._feature_names_for_pipe(pipe, sid)
        x = pd.DataFrame([{k: row.get(k, float("nan")) for k in features}])
        proba = pipe.predict_proba(x)[0]
        p_loss, p_profit = float(proba[0]), float(proba[1])
        pred = "profit" if p_profit >= p_loss else "loss"
        return {
            "predicted": pred,
            "confidence_profit": round(p_profit, 4),
            "confidence_loss": round(p_loss, 4),
            "confidence": round(max(p_profit, p_loss), 4),
            "ready": True,
            "model_scope": scope,
        }

    def _feature_names_for_pipe(self, pipe: Any, scenario_id: str) -> list[str]:
        """Prefer pipeline/meta feature list so wide models work alongside core pack."""
        names = getattr(pipe, "feature_names_in_", None)
        if names is not None and len(names):
            return [str(x) for x in names]
        meta = self._load_scenario_meta(scenario_id) if scenario_id else {}
        feats = meta.get("features")
        if isinstance(feats, list) and feats:
            return [str(x) for x in feats]
        mkt = meta.get("market_features")
        if isinstance(mkt, list) and mkt:
            base = [
                "stake_usdt",
                "stoploss",
                "roi_at_entry",
                "hour_utc",
                "dow_utc",
                "mins_since_armed",
                "log_open_rate",
            ]
            return list(CAT_FEATURES) + base + [str(x) for x in mkt]
        return feature_columns()

    def _resolve_gate_rules(self, scenario: dict[str, Any] | None) -> tuple[str, float]:
        self._refresh_config()
        sid = (scenario or {}).get("scenario_id") or ""
        strategy = (scenario or {}).get("strategy") or ""
        if strategy == FINDER_STRATEGY:
            finder = self.config.get("finder_bots") or {}
            if finder:
                return (
                    finder.get("gate_mode", "profit_only"),
                    float(finder.get("min_confidence") or 0.6),
                )
            return (
                self.config.get("gate_mode", "block_loss"),
                float(self.config.get("min_confidence") or 0.0),
            )
        if sid == GRID_SCENARIO_ID:
            grid = self.config.get("grid_bots") or {}
            if grid:
                return (
                    grid.get("gate_mode", "profit_only"),
                    float(grid.get("min_confidence") or 0.7),
                )
        strat = self.config.get("strategy_bots") or {}
        mode = (strat or {}).get("gate_mode", "profit_only") if strat else self.config.get(
            "gate_mode", "block_loss"
        )
        floor = float((strat or {}).get("min_confidence") or self.config.get("min_confidence") or 0.0)
        # UI override per strategy class (enabled_strategies.json → ml_confidence).
        strategy_cls = (scenario or {}).get("strategy") or ""
        if strategy_cls:
            overrides = load_ml_confidence_overrides(self.user_data)
            if strategy_cls in overrides:
                try:
                    return mode, float(overrides[strategy_cls])
                except (TypeError, ValueError):
                    pass
        # Per-scenario threshold from model meta (experiment winner gate).
        if sid:
            meta = self._load_scenario_meta(sid)
            if meta.get("min_profit_proba") is not None:
                try:
                    return mode, float(meta["min_profit_proba"])
                except (TypeError, ValueError):
                    pass
        if strat:
            return mode, floor if floor else 0.6
        return (
            self.config.get("gate_mode", "block_loss"),
            float(self.config.get("min_confidence") or 0.0),
        )

    def should_block(
        self,
        ml: dict[str, Any],
        *,
        scenario: dict[str, Any] | None = None,
        gate_mode: str | None = None,
        min_confidence: float | None = None,
    ) -> bool:
        if gate_mode is not None and min_confidence is not None:
            mode, min_conf = gate_mode, min_confidence
        else:
            mode, min_conf = self._resolve_gate_rules(scenario)
        # Fail-closed for profit_only when model is unavailable (no silent pass-through).
        if not ml.get("ready", True):
            return mode == "profit_only"
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

    def _log_ml(self, level: str, msg: str, *args) -> None:
        getattr(logger, level)(msg, *args)

    def _fmt_ml(self, ml: dict[str, Any]) -> str:
        cp = float(ml.get("confidence_profit") or 0)
        cl = float(ml.get("confidence_loss") or 0)
        return f"pred={ml.get('predicted')} profit={cp:.1%} loss={cl:.1%}"

    def allow_entry(
        self,
        *,
        scenario: dict[str, Any],
        pair: str,
        rate: float,
        side: str,
        current_time: datetime,
        stake_usdt: float,
        stoploss: float,
        minimal_roi: dict,
        timeframe: str,
        ohlcv_df: pd.DataFrame | None,
    ) -> bool:
        if not self.enabled:
            return True
        market = features_from_ohlcv(ohlcv_df)
        row = self._build_row(
            scenario=scenario,
            pair=pair,
            rate=rate,
            is_short=side == "short",
            current_time=current_time,
            stake_usdt=stake_usdt,
            stoploss=stoploss,
            minimal_roi=minimal_roi,
            timeframe=timeframe,
            market=market,
        )
        ml = self.predict_row(row, scenario_id=scenario.get("scenario_id"))
        mode, min_conf = self._resolve_gate_rules(scenario)
        ml_txt = self._fmt_ml(ml)
        scope = ml.get("model_scope", "global")
        if not ml.get("ready", True):
            self._log_ml(
                "warning",
                "ML gate BLOCK %s %s scenario=%s model not ready mode=%s min=%.0f%%",
                pair,
                side,
                scenario.get("scenario_id"),
                mode,
                min_conf * 100,
            )
            if mode == "profit_only":
                return False
        if self.should_block(ml, scenario=scenario):
            reason = "below_min" if ml.get("predicted") == "profit" else "not_profit"
            if mode == "block_loss" and ml.get("predicted") == "loss":
                reason = "predicted_loss"
            if not ml.get("ready", True):
                reason = "model_not_ready"
            self._log_ml(
                "info",
                "ML gate BLOCK %s %s scenario=%s %s mode=%s min=%.0f%% model=%s reason=%s",
                pair,
                side,
                scenario.get("scenario_id"),
                ml_txt,
                mode,
                min_conf * 100,
                scope,
                reason,
            )
            return False
        self._log_ml(
            "info",
            "ML gate ALLOW %s %s scenario=%s %s mode=%s min=%.0f%% model=%s",
            pair,
            side,
            scenario.get("scenario_id"),
            ml_txt,
            mode,
            min_conf * 100,
            scope,
        )
        if ml.get("ready", True):
            stash_entry_ml(pair, side, scenario, ml)
        return True

    def score_trade_entry(
        self,
        *,
        scenario: dict[str, Any],
        pair: str,
        rate: float,
        side: str,
        current_time: datetime,
        stake_usdt: float,
        stoploss: float,
        minimal_roi: dict,
        timeframe: str,
        ohlcv_df: pd.DataFrame | None,
    ) -> dict[str, Any]:
        """Predict ML confidence for an already-accepted entry (persist to trade)."""
        market = features_from_ohlcv(ohlcv_df)
        row = self._build_row(
            scenario=scenario,
            pair=pair,
            rate=rate,
            is_short=side == "short",
            current_time=current_time,
            stake_usdt=stake_usdt,
            stoploss=stoploss,
            minimal_roi=minimal_roi,
            timeframe=timeframe,
            market=market,
        )
        return self.predict_row(row, scenario_id=scenario.get("scenario_id"))


def get_live_ml_gate() -> LiveMlGate:
    global _GATE
    if _GATE is None:
        _GATE = LiveMlGate()
    return _GATE


def allow_trade_entry(
    *,
    scenario: dict[str, Any],
    pair: str,
    rate: float,
    side: str,
    current_time: datetime,
    stake_usdt: float,
    stoploss: float,
    minimal_roi: dict,
    timeframe: str,
    ohlcv_df: pd.DataFrame | None,
) -> bool:
    return get_live_ml_gate().allow_entry(
        scenario=scenario,
        pair=pair,
        rate=rate,
        side=side,
        current_time=current_time,
        stake_usdt=stake_usdt,
        stoploss=stoploss,
        minimal_roi=minimal_roi,
        timeframe=timeframe,
        ohlcv_df=ohlcv_df,
    )


def _stash_is_usable(ml: dict[str, Any] | None) -> bool:
    """Confirm-time stash is enough even if an older bot omitted ready=True."""
    if not ml:
        return False
    if ml.get("ready"):
        return True
    return ml.get("confidence_profit") is not None or bool(ml.get("predicted"))


def persist_entry_ml(
    trade: Any,
    *,
    scenario: dict[str, Any],
    ohlcv_df: pd.DataFrame | None,
    current_time: datetime,
    stake_usdt: float,
    stoploss: float,
    minimal_roi: dict,
    timeframe: str,
) -> dict[str, Any] | None:
    """Pop stashed confirm-time ML score (preferred) or re-predict, then persist."""
    side = "short" if getattr(trade, "is_short", False) else "long"
    pair = getattr(trade, "pair", "") or ""
    sid = (scenario or {}).get("scenario_id") or ""
    ml = pop_entry_ml(pair, side, sid)
    from_stash = _stash_is_usable(ml)
    if from_stash:
        ml = {**ml, "ready": True, "source": ml.get("source") or "confirm"}
    else:
        gate = get_live_ml_gate()
        rate = float(getattr(trade, "open_rate", 0) or 0)
        ml = gate.score_trade_entry(
            scenario=scenario,
            pair=pair,
            rate=rate,
            side=side,
            current_time=current_time,
            stake_usdt=stake_usdt,
            stoploss=stoploss,
            minimal_roi=minimal_roi,
            timeframe=timeframe,
            ohlcv_df=ohlcv_df,
        )
        if ml:
            ml = {**ml, "source": "fill_repredict"}
            gate._log_ml(
                "warning",
                "ML persist %s %s scenario=%s: no confirm stash, re-predicted "
                "pred=%s profit=%.1f%% (may differ from gate decision)",
                pair,
                side,
                sid,
                ml.get("predicted"),
                float(ml.get("confidence_profit") or 0) * 100,
            )
    if ml and (ml.get("ready") or ml.get("confidence_profit") is not None):
        # Also store the gate threshold that was active for this strategy.
        _, min_conf = get_live_ml_gate()._resolve_gate_rules(scenario)
        ml = {**ml, "min_confidence": min_conf, "ready": True}
        save_ml_to_trade(trade, ml)
        try:
            trade.set_custom_data("ml_min_confidence", float(min_conf))
            src = ml.get("source")
            if src:
                trade.set_custom_data("ml_score_source", str(src))
        except Exception:
            pass
        return ml
    return ml
