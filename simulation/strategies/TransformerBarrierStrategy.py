"""Transformer gate bot: Scalp EMA entries filtered by sequence Transformer."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from pandas import DataFrame

from simulation.strategies.SimScalpingStrategies import ScalpEmaCrossStrategy

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = ROOT / "simulation/data/models/barrier_transformer_5m_gpu.pt"
FALLBACK_MODEL = ROOT / "simulation/data/models/barrier_transformer_5m.pt"


class TransformerBarrierStrategy(ScalpEmaCrossStrategy):
    """EMA 8/21 cross entries gated by XF profit-proba (strategy-gate model)."""

    stoploss = -0.01
    minimal_roi = {"0": 0.008, "20": 0.004, "60": 0}
    pair_cooldown_minutes = 60
    startup_candle_count = 140
    can_short = True

    model_path = os.environ.get(
        "CT_XF_BARRIER_MODEL",
        str(DEFAULT_MODEL if DEFAULT_MODEL.is_file() else FALLBACK_MODEL),
    )
    conf_thr_override = os.environ.get("CT_XF_BARRIER_THR")

    _inferencer = None
    _inferencer_key: str | None = None

    def _get_inferencer(self):
        key = f"{self.model_path}|{self.conf_thr_override}"
        if (
            TransformerBarrierStrategy._inferencer is not None
            and TransformerBarrierStrategy._inferencer_key == key
        ):
            return TransformerBarrierStrategy._inferencer
        path = Path(self.model_path)
        if not path.is_file():
            TransformerBarrierStrategy._inferencer = None
            TransformerBarrierStrategy._inferencer_key = key
            return None
        from simulation.ml.barrier_transformer import BarrierTransformerInferencer

        thr = float(self.conf_thr_override) if self.conf_thr_override else None
        inf = BarrierTransformerInferencer(path, conf_thr=thr)
        # Gate mode: score all EMA-candidate bars, not only vol setups
        inf.require_setup = False
        TransformerBarrierStrategy._inferencer = inf
        TransformerBarrierStrategy._inferencer_key = key
        if inf.sl:
            self.stoploss = -abs(float(inf.sl))
        if inf.tp:
            self.minimal_roi = {"0": float(inf.tp)}
        return inf

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = super().populate_indicators(dataframe, metadata)
        df["xf_p_long"] = np.nan
        df["xf_p_short"] = np.nan
        df["xf_p_tp"] = np.nan
        inf = self._get_inferencer()
        if inf is None or df.empty:
            df["xf_thr"] = 0.55
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
            # Only score near potential crosses to keep runtime sane: every bar with stride 1
            # on last 3 bars would miss history — score stride=3 for backtest speed
            stride = int(os.environ.get("CT_XF_BARRIER_STRIDE", "3"))
            long_p, short_p = inf.score_sides(ohlcv, stride=stride)
            df["xf_p_long"] = long_p.reindex(ohlcv.index).to_numpy(dtype=float)
            df["xf_p_short"] = short_p.reindex(ohlcv.index).to_numpy(dtype=float)
            # forward-fill within stride gaps so EMA cross bars still see a score
            df["xf_p_long"] = df["xf_p_long"].ffill(limit=stride)
            df["xf_p_short"] = df["xf_p_short"].ffill(limit=stride)
            df["xf_p_tp"] = df[["xf_p_long", "xf_p_short"]].max(axis=1)
        except Exception:
            pass

        df["xf_thr"] = float(inf.conf_thr)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        if "xf_p_long" not in dataframe.columns:
            return dataframe
        thr = float(dataframe["xf_thr"].iloc[-1]) if "xf_thr" in dataframe.columns else 0.55
        # Drop EMA entries the gate rejects
        long_ok = dataframe["xf_p_long"].fillna(0) >= thr
        short_ok = dataframe["xf_p_short"].fillna(0) >= thr
        dataframe.loc[~long_ok, "enter_long"] = 0
        dataframe.loc[~short_ok, "enter_short"] = 0
        return dataframe
