"""Live ML trade finder — scan bars for XGBoost entry signals + optional pnl gate."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from ml.gate import get_live_ml_gate
from ml.joblib_compat import register_simulation_stubs
from ml.market_features import MARKET_FEATURES, compute_indicator_frame, min_indicator_warmup

logger = logging.getLogger(__name__)

USER_DATA = Path(__file__).resolve().parent.parent
CONFIG_PATH = USER_DATA / "trade_finder.json"
MODEL_PATH = USER_DATA / "models/trade_finder/trade_finder.joblib"

CAT_FEATURES = ["pair_base", "is_short"]
NUM_FEATURES = ["hour_utc", "dow_utc"] + MARKET_FEATURES

DEFAULT_CFG: dict[str, Any] = {
    "timeframe": "5m",
    "tp_atr_mult": 2.0,
    "sl_atr_mult": 0.8,
    "max_bars": 48,
    "scan_stride": 4,
    "min_confidence": 0.52,
    "stake_usdt": 10.0,
    "cooldown_bars": 12,
    "invert_signal": False,
    "classifier_gate": {
        "enabled": True,
        "scenario_id": "trend_ema",
        "gate_mode": "profit_only",
        "min_confidence": 0.6,
        "armed_offset_ms": 3600000,
    },
}

GATE_SCENARIO: dict[str, dict[str, str]] = {
    "trend_ema": {
        "scenario_id": "trend_ema",
        "scan_type": "strategy",
        "group": "trend",
        "strategy": "TradeFinderStrategy",
        "label": "ML Finder",
    },
}


def pair_base(pair: str) -> str:
    return (pair or "").split("/")[0]


def feature_columns() -> list[str]:
    return CAT_FEATURES + NUM_FEATURES


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return dict(DEFAULT_CFG)
    return {**DEFAULT_CFG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}


class FinderLive:
    def __init__(self) -> None:
        self.cfg = load_config()
        tf = self.cfg.get("timeframe", "5m")
        self.warmup = min_indicator_warmup(tf)
        self.scan_stride = int(self.cfg.get("scan_stride") or 4)
        self.min_confidence = float(self.cfg.get("min_confidence") or 0.52)
        self._pipe = None
        self._armed_at_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        if self.cfg.get("invert_signal"):
            logger.warning("Finder invert_signal=ON — trade direction opposite to model pick")

    def _ensure_model(self) -> bool:
        if self._pipe is not None:
            return True
        if not MODEL_PATH.is_file():
            logger.warning("trade finder model missing: %s", MODEL_PATH)
            return False
        try:
            register_simulation_stubs()
            self._pipe = joblib.load(MODEL_PATH)
            return True
        except Exception as exc:
            logger.warning("trade finder load failed: %s", exc)
            return False

    def predict_row(self, row: dict[str, Any]) -> dict[str, Any]:
        features = feature_columns()
        x = pd.DataFrame([{k: row[k] for k in features}])
        proba = self._pipe.predict_proba(x)[0]
        p_loss, p_profit = float(proba[0]), float(proba[1])
        pred = "profit" if p_profit >= p_loss else "loss"
        return {
            "predicted": pred,
            "confidence_profit": round(p_profit, 4),
            "confidence_loss": round(p_loss, 4),
            "confidence": round(max(p_profit, p_loss), 4),
        }

    def _feature_row(self, pair: str, ts: pd.Timestamp, ind_row: pd.Series, is_short: bool) -> dict[str, Any]:
        row: dict[str, Any] = {
            "pair_base": pair_base(pair),
            "is_short": "1" if is_short else "0",
            "hour_utc": ts.hour,
            "dow_utc": ts.weekday(),
        }
        for col in MARKET_FEATURES:
            val = ind_row.get(col)
            row[col] = float(val) if val is not None and not pd.isna(val) else float("nan")
        return row

    def inst_config(self, atr_pct: float) -> dict[str, Any]:
        sl_mult = float(self.cfg.get("sl_atr_mult") or 0.8)
        tp_mult = float(self.cfg.get("tp_atr_mult") or 2.0)
        ap = max(float(atr_pct or 0), 0.001)
        return {
            "stake_usdt": float(self.cfg.get("stake_usdt") or 10),
            "stoploss": round(-sl_mult * ap, 6),
            "minimal_roi": {"0": round(tp_mult * ap, 6)},
            "timeframe": self.cfg.get("timeframe", "5m"),
            "atr_pct": ap,
            "tp_ratio": round(tp_mult * ap, 6),
        }

    def scan_last_bar(
        self,
        pair: str,
        dataframe: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """Return entry signal for the latest candle, or None."""
        if not self._ensure_model():
            return None
        if dataframe is None or len(dataframe) < self.warmup + 2:
            return None

        ohlcv = dataframe[["open", "high", "low", "close", "volume"]].copy()
        ind = compute_indicator_frame(ohlcv)
        idx = len(ind) - 1
        self.cfg = load_config()
        self.scan_stride = int(self.cfg.get("scan_stride") or 4)
        self.min_confidence = float(self.cfg.get("min_confidence") or 0.52)
        if idx < self.warmup or idx % self.scan_stride != 0:
            return None

        atr_pct = float(ind.iloc[idx].get("atr_pct_14") or 0)
        if atr_pct <= 0 or np.isnan(atr_pct):
            return None

        ts = dataframe["date"].iloc[idx] if "date" in dataframe.columns else ind.index[idx]
        if isinstance(ts, pd.Timestamp) and ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        open_ms = int(pd.Timestamp(ts).timestamp() * 1000)
        rate = float(dataframe["close"].iloc[idx])
        ind_row = ind.iloc[idx]

        best: dict[str, Any] | None = None
        best_reject: dict[str, Any] | None = None
        for is_short in (False, True):
            row = self._feature_row(pair, pd.Timestamp(ts), ind_row, is_short)
            ml = self.predict_row(row)
            side = "short" if is_short else "long"
            if ml["predicted"] != "profit" or ml["confidence_profit"] < self.min_confidence:
                if best_reject is None or ml["confidence_profit"] > best_reject["ml"]["confidence_profit"]:
                    best_reject = {"is_short": is_short, "ml": ml, "side": side}
                logger.debug(
                    "Finder skip %s %s pred=%s profit=%.1f%% (min %.0f%%)",
                    pair,
                    side,
                    ml["predicted"],
                    ml["confidence_profit"] * 100,
                    self.min_confidence * 100,
                )
                continue
            if best is None or ml["confidence_profit"] > best["ml"]["confidence_profit"]:
                best = {"is_short": is_short, "ml": ml, "open_ms": open_ms, "rate": rate}

        if best is None:
            if best_reject is not None:
                br = best_reject["ml"]
                logger.info(
                    "Finder no signal %s best_%s profit=%.1f%% pred=%s (min %.0f%%)",
                    pair,
                    best_reject["side"],
                    br["confidence_profit"] * 100,
                    br["predicted"],
                    self.min_confidence * 100,
                )
            return None

        invert = bool(self.cfg.get("invert_signal"))
        model_side = "short" if best["is_short"] else "long"
        if invert:
            best["is_short"] = not best["is_short"]
            best["finder_model_side"] = model_side

        trade_side = "short" if best["is_short"] else "long"
        inv_note = f" INVERTED (model={model_side})" if invert else ""
        logger.info(
            "Finder signal %s %s%s profit=%.1f%% loss=%.1f%%",
            pair,
            trade_side,
            inv_note,
            best["ml"]["confidence_profit"] * 100,
            best["ml"]["confidence_loss"] * 100,
        )

        inst = self.inst_config(atr_pct)
        return {
            "pair": pair,
            "side": trade_side,
            "is_short": best["is_short"],
            "open_ms": best["open_ms"],
            "rate": best["rate"],
            "atr_pct": atr_pct,
            "finder_ml": best["ml"],
            "finder_model_side": best.get("finder_model_side"),
            "inverted": invert,
            "inst": inst,
            "cooldown_ms": int(self.cfg.get("cooldown_bars") or 12) * 5 * 60 * 1000,
        }

    def allow_with_classifier_gate(
        self,
        signal: dict[str, Any],
        *,
        current_time: datetime,
        ohlcv_df: pd.DataFrame | None,
    ) -> bool:
        gate_cfg = self.cfg.get("classifier_gate") or {}
        if not gate_cfg.get("enabled", True):
            return True
        sid = gate_cfg.get("scenario_id", "trend_ema")
        scenario = GATE_SCENARIO.get(sid, GATE_SCENARIO["trend_ema"])
        inst = signal["inst"]
        gate = get_live_ml_gate()
        armed = int(signal["open_ms"]) - int(gate_cfg.get("armed_offset_ms") or 3600000)
        gate._armed_at_ms = armed  # noqa: SLF001 — per-signal armed window
        row = gate._build_row(
            scenario=scenario,
            pair=signal["pair"],
            rate=signal["rate"],
            is_short=signal["is_short"],
            current_time=current_time,
            stake_usdt=inst["stake_usdt"],
            stoploss=inst["stoploss"],
            minimal_roi=inst["minimal_roi"],
            timeframe=inst.get("timeframe", "5m"),
            market=signal.get("market") or {},
        )
        if ohlcv_df is not None and not ohlcv_df.empty:
            from ml.market_features import features_from_ohlcv

            mkt = features_from_ohlcv(ohlcv_df)
            for col in MARKET_FEATURES:
                row[col] = mkt.get(col, row.get(col, float("nan")))
        ml = gate.predict_row(row)
        signal["gate_ml"] = ml
        mode, min_conf = gate._resolve_gate_rules(scenario)  # noqa: SLF001
        allowed = not gate.should_block(ml, scenario=scenario, gate_mode=mode, min_confidence=min_conf)
        cp = float(ml.get("confidence_profit") or 0)
        cl = float(ml.get("confidence_loss") or 0)
        if allowed:
            logger.info(
                "Finder classifier ALLOW %s profit=%.1f%% loss=%.1f%% pred=%s",
                signal["pair"],
                cp * 100,
                cl * 100,
                ml.get("predicted"),
            )
        else:
            logger.info(
                "Finder classifier BLOCK %s profit=%.1f%% loss=%.1f%% pred=%s",
                signal["pair"],
                cp * 100,
                cl * 100,
                ml.get("predicted"),
            )
        return allowed
