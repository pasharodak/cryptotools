#!/usr/bin/env python3
"""4H trend RNN (GRU): predict forward direction from a window of 4h bars.

Label: sign of close[t+horizon] / close[t] - 1  (with flat band).
Features per 4h bar: returns, range, volume change, EMA spreads, ADX-ish.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from simulation.ml.trend_4h import Trend4hModel, resample_ohlcv


FEATURE_COLS = [
    "ret_1",
    "ret_3",
    "ret_6",
    "range_pct",
    "body_pct",
    "vol_chg",
    "ema_spread_20_50",
    "ema_spread_50_200",
    "dist_ema50",
    "adx_n",
    "di_spread",
    "rule_score",
]


def build_feature_frame(ohlcv_any_tf: pd.DataFrame) -> pd.DataFrame:
    """Resample to 4h and compute model features (+ rule score)."""
    model = Trend4hModel()
    h4 = model.build_4h_frame(ohlcv_any_tf)
    if h4.empty or len(h4) < 80:
        return pd.DataFrame()

    c = h4["close"].astype(float)
    h = h4["high"].astype(float)
    l = h4["low"].astype(float)
    o = h4["open"].astype(float)
    v = h4["volume"].astype(float) if "volume" in h4.columns else pd.Series(1.0, index=h4.index)

    ema20 = h4.get("ema20", c.ewm(span=20, adjust=False).mean())
    ema50 = h4.get("ema50", c.ewm(span=50, adjust=False).mean())
    ema200 = h4.get("ema200", c.ewm(span=200, adjust=False).mean())

    feat = pd.DataFrame(index=h4.index)
    feat["ret_1"] = c.pct_change(1)
    feat["ret_3"] = c.pct_change(3)
    feat["ret_6"] = c.pct_change(6)
    feat["range_pct"] = (h - l) / c.replace(0, np.nan)
    feat["body_pct"] = (c - o) / c.replace(0, np.nan)
    feat["vol_chg"] = np.log(v.replace(0, np.nan)).diff()
    feat["ema_spread_20_50"] = (ema20 - ema50) / c.replace(0, np.nan)
    feat["ema_spread_50_200"] = (ema50 - ema200) / c.replace(0, np.nan)
    feat["dist_ema50"] = (c - ema50) / c.replace(0, np.nan)
    adx = h4["trend_4h_adx"].astype(float)
    feat["adx_n"] = (adx / 50.0).clip(0, 2)
    feat["di_spread"] = ((h4["di_plus"] - h4["di_minus"]) / 50.0).clip(-2, 2)
    feat["rule_score"] = h4["trend_4h_score"].astype(float)
    feat["close"] = c
    return feat.replace([np.inf, -np.inf], np.nan)


def make_sequences(
    feat: pd.DataFrame,
    *,
    window: int,
    horizon: int,
    flat_band: float = 0.002,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return X [N,T,F], y class {0,1,2}=down/flat/up, fwd_ret [N]."""
    df = feat.dropna(subset=FEATURE_COLS + ["close"])
    if len(df) < window + horizon + 10:
        return (
            np.zeros((0, window, len(FEATURE_COLS)), dtype=np.float32),
            np.zeros((0,), dtype=np.int64),
            np.zeros((0,), dtype=np.float32),
        )

    x_mat = df[FEATURE_COLS].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    # z-score features per column on the fly later (global train stats)

    xs, ys, rets = [], [], []
    for i in range(window - 1, len(df) - horizon):
        fwd = close[i + horizon] / close[i] - 1.0
        if abs(fwd) < flat_band:
            lab = 1  # flat
        elif fwd > 0:
            lab = 2  # up
        else:
            lab = 0  # down
        xs.append(x_mat[i - window + 1 : i + 1])
        ys.append(lab)
        rets.append(fwd)
    return (
        np.asarray(xs, dtype=np.float32),
        np.asarray(ys, dtype=np.int64),
        np.asarray(rets, dtype=np.float32),
    )


class Trend4hGRU(nn.Module):
    def __init__(self, n_feat: int = len(FEATURE_COLS), hidden: int = 64, layers: int = 2, dropout: float = 0.2):
        super().__init__()
        self.gru = nn.GRU(
            input_size=n_feat,
            hidden_size=hidden,
            num_layers=layers,
            batch_first=True,
            dropout=dropout if layers > 1 else 0.0,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 3),  # down / flat / up
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.gru(x)
        return self.head(out[:, -1, :])


@dataclass
class TrainResult:
    model: Trend4hGRU
    mean: np.ndarray
    std: np.ndarray
    metrics: dict[str, Any]
    device: str


def _normalize(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return ((x - mean) / (std + 1e-8)).astype(np.float32)


def train_trend_gru(
    X: np.ndarray,
    y: np.ndarray,
    fwd: np.ndarray,
    *,
    epochs: int = 12,
    batch: int = 256,
    lr: float = 1e-3,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
    device: torch.device | None = None,
    seed: int = 42,
) -> TrainResult:
    """Time-ordered split: train → val → test."""
    device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(seed)
    n = len(y)
    n_test = max(1, int(n * test_frac))
    n_val = max(1, int(n * val_frac))
    n_train = n - n_val - n_test
    if n_train < 200:
        raise ValueError(f"not enough samples to train: {n}")

    X_tr, y_tr = X[:n_train], y[:n_train]
    X_va, y_va, f_va = X[n_train : n_train + n_val], y[n_train : n_train + n_val], fwd[n_train : n_train + n_val]
    X_te, y_te, f_te = X[n_train + n_val :], y[n_train + n_val :], fwd[n_train + n_val :]

    mean = X_tr.reshape(-1, X_tr.shape[-1]).mean(axis=0)
    std = X_tr.reshape(-1, X_tr.shape[-1]).std(axis=0)
    X_tr_n = _normalize(X_tr, mean, std)
    X_va_n = _normalize(X_va, mean, std)
    X_te_n = _normalize(X_te, mean, std)

    # class weights
    counts = np.bincount(y_tr, minlength=3).astype(np.float64)
    weights = counts.sum() / (counts + 1e-6)
    weights = weights / weights.mean()
    w = torch.tensor(weights, dtype=torch.float32, device=device)

    model = Trend4hGRU().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss(weight=w)

    tr_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_tr_n), torch.from_numpy(y_tr)),
        batch_size=batch,
        shuffle=True,
    )
    va_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_va_n), torch.from_numpy(y_va)),
        batch_size=batch,
        shuffle=False,
    )

    best_state = None
    best_va = -1.0
    for ep in range(epochs):
        model.train()
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        va_hit = _directional_hit(model, va_loader, device, f_va if False else None, y_va)
        # use class accuracy on val for early pick
        va_acc = _accuracy(model, va_loader, device)
        if va_acc > best_va:
            best_va = va_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(f"epoch {ep+1}/{epochs} val_acc={va_acc:.3f} best={best_va:.3f}")

    if best_state:
        model.load_state_dict(best_state)
    model.to(device)

    te_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_te_n), torch.from_numpy(y_te)),
        batch_size=batch,
        shuffle=False,
    )
    metrics = evaluate_predictions(model, te_loader, device, y_te, f_te)
    metrics["val_acc"] = round(best_va, 4)
    metrics["n_train"] = int(n_train)
    metrics["n_val"] = int(n_val)
    metrics["n_test"] = int(len(y_te))
    metrics["class_counts_train"] = counts.astype(int).tolist()
    return TrainResult(model=model, mean=mean, std=std, metrics=metrics, device=str(device))


@torch.no_grad()
def _accuracy(model: nn.Module, loader: DataLoader, device: torch.device) -> float:
    model.eval()
    correct = 0
    total = 0
    for xb, yb in loader:
        pred = model(xb.to(device)).argmax(dim=1).cpu()
        correct += int((pred == yb).sum())
        total += len(yb)
    return correct / max(total, 1)


@torch.no_grad()
def _directional_hit(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    fwd: np.ndarray | None,
    y: np.ndarray,
) -> float:
    del fwd, y
    return _accuracy(model, loader, device)


@torch.no_grad()
def evaluate_predictions(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    y_true: np.ndarray,
    fwd: np.ndarray,
) -> dict[str, Any]:
    model.eval()
    preds = []
    for xb, _ in loader:
        logits = model(xb.to(device))
        preds.append(logits.argmax(dim=1).cpu().numpy())
    pred = np.concatenate(preds)
    # Map class → trade dir: 0→-1, 1→0, 2→+1
    trade_dir = np.where(pred == 2, 1, np.where(pred == 0, -1, 0))
    # directional hit among non-flat predictions
    mask = trade_dir != 0
    if mask.sum() == 0:
        hit = None
        avg_signed = None
        n_traded = 0
    else:
        signed = trade_dir[mask] * fwd[mask]
        hit = float(((fwd[mask] > 0) & (trade_dir[mask] > 0)).sum() + ((fwd[mask] < 0) & (trade_dir[mask] < 0)).sum()) / float(
            mask.sum()
        )
        avg_signed = float(signed.mean())
        n_traded = int(mask.sum())

    # also: force up/down only by ignoring flat class — use argmax among {0,2}
    # already using 3-class

    acc = float((pred == y_true).mean())
    # baseline always-long among all
    base_hit = float((fwd > 0).mean())
    return {
        "test_acc_3class": round(acc * 100, 2),
        "trade_hit_rate": round(hit * 100, 2) if hit is not None else None,
        "trade_avg_signed_pct": round(avg_signed * 100, 4) if avg_signed is not None else None,
        "n_traded": n_traded,
        "pct_flat_pred": round(100 * float((trade_dir == 0).mean()), 1),
        "baseline_long_hit": round(base_hit * 100, 2),
        "pred_counts": np.bincount(pred, minlength=3).tolist(),
        "true_counts": np.bincount(y_true, minlength=3).tolist(),
    }


def predict_dirs(
    model: Trend4hGRU,
    X: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
    device: torch.device,
    batch: int = 512,
) -> np.ndarray:
    """Return trade dirs {-1,0,1} for each sequence."""
    model.eval()
    Xn = _normalize(X, mean, std)
    dirs = []
    with torch.no_grad():
        for i in range(0, len(Xn), batch):
            xb = torch.from_numpy(Xn[i : i + batch]).to(device)
            pred = model(xb).argmax(dim=1).cpu().numpy()
            d = np.where(pred == 2, 1, np.where(pred == 0, -1, 0))
            dirs.append(d)
    return np.concatenate(dirs) if dirs else np.zeros((0,), dtype=int)
