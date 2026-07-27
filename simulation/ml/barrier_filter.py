#!/usr/bin/env python3
"""Inference helper for asymmetric barrier GRU (TP before SL filter)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from simulation.ml.trend_4h import Trend4hModel
from simulation.ml.trend_4h_rnn import _normalize, build_feature_frame
from simulation.scripts.train_eval_trend_4h_rnn_80 import EXT_COLS, Trend4hGRUv2, _attach_btc


class BarrierTrendFilter:
    """Returns whether to take a trade given recent OHLCV (any TF resampled to 4h)."""

    def __init__(self, model_path: str | Path, *, conf_thr: float = 0.60, device: str | None = None):
        path = Path(model_path)
        blob = torch.load(path, map_location="cpu", weights_only=False)
        self.mean = np.asarray(blob["mean"], dtype=np.float64)
        self.std = np.asarray(blob["std"], dtype=np.float64)
        self.window = int(blob["window"])
        self.tp = float(blob["tp"])
        self.sl = float(blob["sl"])
        self.conf_thr = conf_thr
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.model = Trend4hGRUv2(n_feat=len(EXT_COLS)).to(self.device)
        self.model.load_state_dict(blob["state_dict"])
        self.model.eval()
        self._btc_feat: pd.DataFrame | None = None

    def set_btc(self, btc_ohlcv: pd.DataFrame) -> None:
        self._btc_feat = build_feature_frame(btc_ohlcv)

    @torch.no_grad()
    def decide(self, pair_ohlcv: pd.DataFrame) -> dict[str, Any]:
        feat = build_feature_frame(pair_ohlcv)
        if self._btc_feat is not None:
            feat = _attach_btc(feat, self._btc_feat)
        for c in EXT_COLS:
            if c not in feat.columns:
                return {"take": False, "reason": f"missing {c}"}
        df = feat.dropna(subset=EXT_COLS + ["close", "rule_score"])
        if len(df) < self.window:
            return {"take": False, "reason": "short_history"}
        rule = float(df["rule_score"].iloc[-1])
        if rule >= 0.18:
            side = "long"
        elif rule <= -0.18:
            side = "short"
        else:
            return {"take": False, "reason": "range", "rule_score": rule, "side": None}

        x = df[EXT_COLS].to_numpy(dtype=np.float32)[-self.window :][None, ...]
        xn = _normalize(x, self.mean, self.std)
        logits = self.model(torch.from_numpy(xn).to(self.device))
        prob = torch.softmax(logits, dim=1).cpu().numpy()[0]
        conf = float(prob.max())
        pred_tp = int(prob.argmax()) == 1
        take = bool(pred_tp and conf >= self.conf_thr)
        return {
            "take": take,
            "side": side,
            "rule_score": round(rule, 4),
            "p_tp_first": round(float(prob[1]), 4),
            "conf": round(conf, 4),
            "tp": self.tp,
            "sl": self.sl,
            "reason": "ok" if take else ("low_conf" if pred_tp else "pred_sl"),
        }
