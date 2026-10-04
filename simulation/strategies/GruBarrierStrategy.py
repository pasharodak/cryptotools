"""GRU seq-gate bot: Scalp EMA entries filtered by trained strategy-gate GRU."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from pandas import DataFrame

from simulation.strategies.SimScalpingStrategies import ScalpEmaCrossStrategy

ROOT = Path(__file__).resolve().parents[2]
_CANDIDATES = [
    ROOT / "simulation/data/models/seq_gate_gru.pt",
    ROOT / "site/user_data/models/seq_gate_gru/seq_gate_gru.pt",
    Path(__file__).resolve().parents[1] / "user_data/models/seq_gate_gru/seq_gate_gru.pt",
]
DEFAULT_MODEL = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])


class GruBarrierStrategy(ScalpEmaCrossStrategy):
    """EMA 8/21 cross + seq-gate GRU (compare leader, thr≈0.85)."""

    stoploss = -0.01
    minimal_roi = {"0": 0.008, "20": 0.004, "60": 0}
    pair_cooldown_minutes = 60
    startup_candle_count = 140
    can_short = True
    # Built-in neural gate — MultiStrategyRouter skips LightGBM for this tag.
    builtin_seq_gate = True

    model_path = os.environ.get("CT_GRU_GATE_MODEL", str(DEFAULT_MODEL))
    conf_thr_override = os.environ.get("CT_GRU_GATE_THR")

    _inferencer = None
    _inferencer_key: str | None = None

    def _get_inferencer(self):
        key = f"{self.model_path}|{self.conf_thr_override}"
        if GruBarrierStrategy._inferencer is not None and GruBarrierStrategy._inferencer_key == key:
            return GruBarrierStrategy._inferencer
        path = Path(self.model_path)
        if not path.is_file():
            GruBarrierStrategy._inferencer = None
            GruBarrierStrategy._inferencer_key = key
            return None
        from simulation.ml.seq_gate_models import SeqGateInferencer

        thr = float(self.conf_thr_override) if self.conf_thr_override else None
        inf = SeqGateInferencer(path, conf_thr=thr)
        GruBarrierStrategy._inferencer = inf
        GruBarrierStrategy._inferencer_key = key
        if inf.sl:
            self.stoploss = -abs(float(inf.sl))
        if inf.tp:
            self.minimal_roi = {"0": float(inf.tp)}
        return inf

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = super().populate_indicators(dataframe, metadata)
        df["gru_p_long"] = np.nan
        df["gru_p_short"] = np.nan
        df["gru_p_tp"] = np.nan
        inf = self._get_inferencer()
        if inf is None or df.empty:
            df["gru_thr"] = 0.85
            return df

        ohlcv = df[["open", "high", "low", "close", "volume"]].copy()
        if "date" in df.columns:
            idx = df["date"]
            try:
                if getattr(idx.dt, "tz", None) is None:
                    idx = idx.dt.tz_localize("UTC")
                else:
                    idx = idx.dt.tz_convert("UTC")
            except (TypeError, AttributeError, ValueError):
                pass
            ohlcv.index = idx

        try:
            stride = int(os.environ.get("CT_GRU_GATE_STRIDE", "3"))
            long_p, short_p = inf.score_sides(ohlcv, stride=stride)
            df["gru_p_long"] = long_p.reindex(ohlcv.index).to_numpy(dtype=float)
            df["gru_p_short"] = short_p.reindex(ohlcv.index).to_numpy(dtype=float)
            df["gru_p_long"] = df["gru_p_long"].ffill(limit=stride)
            df["gru_p_short"] = df["gru_p_short"].ffill(limit=stride)
            df["gru_p_tp"] = df[["gru_p_long", "gru_p_short"]].max(axis=1)
        except Exception:
            pass

        df["gru_thr"] = float(inf.conf_thr)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        if "gru_p_long" not in dataframe.columns:
            return dataframe
        thr = float(dataframe["gru_thr"].iloc[-1]) if "gru_thr" in dataframe.columns else 0.85
        long_ok = dataframe["gru_p_long"].fillna(0) >= thr
        short_ok = dataframe["gru_p_short"].fillna(0) >= thr
        dataframe.loc[~long_ok, "enter_long"] = 0
        dataframe.loc[~short_ok, "enter_short"] = 0
        return dataframe
