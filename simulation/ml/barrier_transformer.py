"""5m sequence Transformer: predict TP-before-SL (triple barrier), v2.

Improvements vs v1:
- ATR-scaled barriers (pair-adaptive)
- Long + short with side channel
- Setup filter (vol/ATR) — train & trade only on candidates
- BTC context features
- Focal loss + expectancy-based threshold pick
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from simulation.ml.ts_entry_features import SEQ_FEATURE_COLS, compute_ts_feature_frame

BASE_FEATURE_COLS = list(SEQ_FEATURE_COLS) + [
    "ets_level_dev",
    "ets_trend_12_48",
    "ets_forecast_ret",
    "vol_z_20",
    "garch_ewma_vol_z",
    "ar1_forecast_ret",
    "atr_pct",
    "atr_z",
    "btc_ret_1",
    "btc_ret_3",
    "vs_btc_ret_1",
    "side",  # +1 long / -1 short (constant over window)
]

FEATURE_COLS = list(BASE_FEATURE_COLS)

DEFAULT_TP_MULT = 1.0
DEFAULT_SL_MULT = 1.0
DEFAULT_HORIZON = 36
DEFAULT_WINDOW = 64
DEFAULT_VOL_Z_MIN = 0.35
DEFAULT_ATR_PCT_MIN = 0.0035


def barrier_outcome(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_pct: np.ndarray,
    i: int,
    *,
    side: int,
    tp_mult: float,
    sl_mult: float,
    max_bars: int,
) -> tuple[int, float]:
    """label=1 if TP before SL; realized approx return (signed for side)."""
    entry = float(close[i])
    ap = float(atr_pct[i])
    if entry <= 0 or not np.isfinite(entry) or not np.isfinite(ap) or ap <= 0:
        return 0, 0.0
    atr = ap * entry
    if side > 0:
        tp_px = entry + tp_mult * atr
        sl_px = entry - sl_mult * atr
        tp_ret, sl_ret = tp_mult * ap, -sl_mult * ap
    else:
        tp_px = entry - tp_mult * atr
        sl_px = entry + sl_mult * atr
        tp_ret, sl_ret = tp_mult * ap, -sl_mult * ap

    end = min(len(close) - 1, i + max_bars)
    for j in range(i + 1, end + 1):
        hi, lo = float(high[j]), float(low[j])
        if side > 0:
            hit_tp, hit_sl = hi >= tp_px, lo <= sl_px
        else:
            hit_tp, hit_sl = lo <= tp_px, hi >= sl_px
        if hit_tp and hit_sl:
            return 0, float(sl_ret)
        if hit_tp:
            return 1, float(tp_ret)
        if hit_sl:
            return 0, float(sl_ret)
    exit_px = float(close[end])
    ret = side * (exit_px / entry - 1.0)
    return (1 if ret > 0 else 0), float(ret)


def _atr_pct(ohlcv: pd.DataFrame, period: int = 14) -> pd.Series:
    h = ohlcv["high"].astype(float)
    l = ohlcv["low"].astype(float)
    c = ohlcv["close"].astype(float)
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=max(5, period // 2)).mean()
    return (atr / c.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)


def build_feature_frame(ohlcv: pd.DataFrame, btc_ohlcv: pd.DataFrame | None = None) -> pd.DataFrame:
    """Causal TS + ATR + optional BTC context; keeps OHLC for barriers."""
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()
    ts = compute_ts_feature_frame(ohlcv)
    out = ts.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in ohlcv.columns:
            out[col] = ohlcv[col].astype(float)
    out["atr_pct"] = _atr_pct(ohlcv)
    atr_ma = out["atr_pct"].rolling(96, min_periods=20).mean()
    out["atr_z"] = out["atr_pct"] / atr_ma.replace(0, np.nan)

    out["btc_ret_1"] = 0.0
    out["btc_ret_3"] = 0.0
    out["vs_btc_ret_1"] = out.get("seq_ret", pd.Series(0.0, index=out.index))
    if btc_ohlcv is not None and not btc_ohlcv.empty:
        btc_ts = compute_ts_feature_frame(btc_ohlcv)
        b = btc_ts.reindex(out.index, method="ffill")
        out["btc_ret_1"] = b["seq_ret"]
        out["btc_ret_3"] = b["seq_ret"].rolling(3, min_periods=1).sum()
        out["vs_btc_ret_1"] = out["seq_ret"] - b["seq_ret"]

    out["side"] = 1.0  # overwritten per-sample when building sequences
    return out.replace([np.inf, -np.inf], np.nan)


def is_setup_row(row: pd.Series, *, vol_z_min: float, atr_pct_min: float) -> bool:
    vz = float(row.get("vol_z_20", np.nan))
    ap = float(row.get("atr_pct", np.nan))
    if not np.isfinite(vz) or not np.isfinite(ap):
        return False
    return vz >= vol_z_min and ap >= atr_pct_min


def make_barrier_sequences(
    feat: pd.DataFrame,
    *,
    window: int = DEFAULT_WINDOW,
    tp_mult: float = DEFAULT_TP_MULT,
    sl_mult: float = DEFAULT_SL_MULT,
    horizon: int = DEFAULT_HORIZON,
    stride: int = 6,
    sides: tuple[int, ...] = (1, -1),
    vol_z_min: float = DEFAULT_VOL_Z_MIN,
    atr_pct_min: float = DEFAULT_ATR_PCT_MIN,
    require_setup: bool = True,
    feature_cols: list[str] | None = None,
    # legacy aliases
    tp: float | None = None,
    sl: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return X, y, realized, end_ms, sides.

    If legacy ``tp``/``sl`` fixed pct are passed (and tp_mult unused path),
    ATR barrier uses atr_pct as unit with mult = tp/mean_atr approx via fixed:
    we interpret tp/sl as fixed percent barriers when ``tp`` is not None.
    """
    cols = feature_cols or FEATURE_COLS
    need = [c for c in cols if c != "side"] + ["high", "low", "close", "atr_pct", "vol_z_20"]
    df = feat.dropna(subset=[c for c in need if c in feat.columns]).copy()
    missing = [c for c in cols if c != "side" and c not in df.columns]
    empty = (
        np.zeros((0, window, len(cols)), dtype=np.float32),
        np.zeros((0,), dtype=np.int64),
        np.zeros((0,), dtype=np.float32),
        np.zeros((0,), dtype=np.int64),
        np.zeros((0,), dtype=np.int64),
    )
    if missing or len(df) < window + horizon + 10:
        return empty

    use_fixed = tp is not None and sl is not None
    base_cols = [c for c in cols if c != "side"]
    x_mat = df[base_cols].to_numpy(dtype=np.float64)
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    atr_pct = df["atr_pct"].to_numpy(dtype=np.float64)
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")

    xs, ys, rets, times, side_arr = [], [], [], [], []
    start_i = window - 1
    end_i = len(df) - horizon - 1

    for i in range(start_i, end_i + 1, max(1, stride)):
        if require_setup and not is_setup_row(df.iloc[i], vol_z_min=vol_z_min, atr_pct_min=atr_pct_min):
            continue
        chunk = np.nan_to_num(x_mat[i - window + 1 : i + 1], nan=0.0, posinf=0.0, neginf=0.0)
        for side in sides:
            if use_fixed:
                lab, real = barrier_outcome(
                    high,
                    low,
                    close,
                    np.ones_like(atr_pct),
                    i,
                    side=side,
                    tp_mult=float(tp),
                    sl_mult=float(sl),
                    max_bars=horizon,
                )
            else:
                lab, real = barrier_outcome(
                    high,
                    low,
                    close,
                    atr_pct,
                    i,
                    side=side,
                    tp_mult=tp_mult,
                    sl_mult=sl_mult,
                    max_bars=horizon,
                )
            ordered = np.zeros((window, len(cols)), dtype=np.float32)
            for j, name in enumerate(cols):
                if name == "side":
                    ordered[:, j] = float(side)
                else:
                    ordered[:, j] = chunk[:, base_cols.index(name)].astype(np.float32)
            xs.append(ordered)
            ys.append(lab)
            rets.append(real)
            times.append(int(pd.Timestamp(idx[i]).timestamp() * 1000))
            side_arr.append(int(side))

    if not xs:
        return empty
    return (
        np.asarray(xs, dtype=np.float32),
        np.asarray(ys, dtype=np.int64),
        np.asarray(rets, dtype=np.float32),
        np.asarray(times, dtype=np.int64),
        np.asarray(side_arr, dtype=np.int64),
    )


def sequences_at_open_ms(
    feat: pd.DataFrame,
    open_ms_list: list[int],
    sides: list[int],
    *,
    window: int = DEFAULT_WINDOW,
    feature_cols: list[str] | None = None,
) -> np.ndarray:
    """Build [N,T,F] windows ending at each open_ms (pad zeros if short)."""
    cols = feature_cols or FEATURE_COLS
    base = [c for c in cols if c != "side"]
    n = len(open_ms_list)
    out = np.zeros((n, window, len(cols)), dtype=np.float32)
    if feat is None or feat.empty or n == 0:
        return out
    df = feat.dropna(subset=[c for c in base if c in feat.columns])
    if df.empty:
        return out
    mat = np.nan_to_num(df[base].to_numpy(dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    for i, oms in enumerate(open_ms_list):
        ts = pd.Timestamp(int(oms), unit="ms", tz="UTC")
        pos = idx.get_indexer([ts], method="pad")[0]
        if pos < 0:
            continue
        start = max(0, pos - window + 1)
        chunk = mat[start : pos + 1]
        side = float(sides[i] if i < len(sides) else 1)
        ordered = np.zeros((window, len(cols)), dtype=np.float32)
        for j, name in enumerate(cols):
            if name == "side":
                ordered[:, j] = side
            else:
                col_i = base.index(name)
                ordered[window - len(chunk) :, j] = chunk[:, col_i].astype(np.float32)
        out[i] = ordered
    return out


def last_window(
    feat: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    *,
    side: int = 1,
    feature_cols: list[str] | None = None,
) -> np.ndarray | None:
    cols = feature_cols or FEATURE_COLS
    base = [c for c in cols if c != "side"]
    df = feat.dropna(subset=[c for c in base if c in feat.columns])
    if len(df) < window:
        return None
    chunk = df[base].to_numpy(dtype=np.float64)[-window:]
    chunk = np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0)
    ordered = np.zeros((1, window, len(cols)), dtype=np.float32)
    for j, name in enumerate(cols):
        if name == "side":
            ordered[0, :, j] = float(side)
        else:
            ordered[0, :, j] = chunk[:, base.index(name)]
    return ordered


def windows_all(
    feat: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    *,
    side: int = 1,
    feature_cols: list[str] | None = None,
    require_setup: bool = True,
    vol_z_min: float = DEFAULT_VOL_Z_MIN,
    atr_pct_min: float = DEFAULT_ATR_PCT_MIN,
    stride: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    cols = feature_cols or FEATURE_COLS
    base = [c for c in cols if c != "side"]
    df = feat.dropna(subset=[c for c in base if c in feat.columns])
    if len(df) < window:
        return np.zeros((0, window, len(cols)), dtype=np.float32), np.zeros((0,), dtype=np.int64)
    mat = np.nan_to_num(df[base].to_numpy(dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    positions = []
    for pos in range(window - 1, len(df), max(1, stride)):
        if require_setup and not is_setup_row(df.iloc[pos], vol_z_min=vol_z_min, atr_pct_min=atr_pct_min):
            continue
        positions.append(pos)
    if not positions:
        return np.zeros((0, window, len(cols)), dtype=np.float32), np.zeros((0,), dtype=np.int64)
    xs = np.zeros((len(positions), window, len(cols)), dtype=np.float32)
    for i, pos in enumerate(positions):
        chunk = mat[pos - window + 1 : pos + 1]
        for j, name in enumerate(cols):
            if name == "side":
                xs[i, :, j] = float(side)
            else:
                xs[i, :, j] = chunk[:, base.index(name)]
    return xs, np.asarray(positions, dtype=np.int64)


class TinyBarrierTransformer(nn.Module):
    def __init__(
        self,
        n_feat: int,
        *,
        d_model: int = 192,
        nhead: int = 8,
        layers: int = 4,
        dim_ff: int | None = None,
        dropout: float = 0.15,
        max_len: int = 192,
    ):
        super().__init__()
        if d_model % nhead != 0:
            raise ValueError(f"d_model={d_model} must be divisible by nhead={nhead}")
        dim_ff = int(dim_ff if dim_ff is not None else 4 * d_model)
        self.d_model = d_model
        self.nhead = nhead
        self.layers = layers
        self.dim_ff = dim_ff
        self.in_proj = nn.Sequential(nn.Linear(n_feat, d_model), nn.LayerNorm(d_model))
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div)
        pe[:, 1::2] = torch.cos(position * div)
        self.register_buffer("pe", pe.unsqueeze(0), persistent=False)
        enc_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=dim_ff,
            batch_first=True,
            dropout=dropout,
            activation="gelu",
        )
        self.enc = nn.TransformerEncoder(enc_layer, num_layers=layers)
        self.head = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_model // 2, 2),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.in_proj(x)
        t = h.size(1)
        h = h + self.pe[:, :t, :]
        h = self.enc(h)
        pooled = 0.5 * (h.mean(dim=1) + h[:, -1, :])
        return self.head(pooled)


class FocalLoss(nn.Module):
    def __init__(self, weight: torch.Tensor | None = None, gamma: float = 1.5):
        super().__init__()
        self.weight = weight
        self.gamma = gamma

    def forward(self, logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        ce = F.cross_entropy(logits, target, weight=self.weight, reduction="none")
        pt = torch.exp(-ce)
        return ((1 - pt) ** self.gamma * ce).mean()


def _normalize(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return ((x - mean) / (std + 1e-8)).astype(np.float32)


@dataclass
class BarrierTrainResult:
    model: TinyBarrierTransformer
    mean: np.ndarray
    std: np.ndarray
    temperature: float
    metrics: dict[str, Any]
    device: str


@torch.no_grad()
def predict_proba(
    model: nn.Module,
    X: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
    *,
    device: torch.device,
    batch: int = 512,
    temperature: float = 1.0,
) -> np.ndarray:
    model.eval()
    if len(X) == 0:
        return np.zeros((0,), dtype=np.float64)
    xn = _normalize(X, mean, std)
    out = []
    temp = max(float(temperature), 1e-3)
    for i in range(0, len(xn), batch):
        xb = torch.from_numpy(xn[i : i + batch]).to(device)
        logits = model(xb) / temp
        p = torch.softmax(logits, dim=1)[:, 1].cpu().numpy()
        out.append(p)
    return np.concatenate(out).astype(np.float64)


def fit_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    if len(y) < 50 or len(np.unique(y)) < 2:
        return 1.0
    t = torch.nn.Parameter(torch.ones(1))
    y_t = torch.from_numpy(y.astype(np.int64))
    logits_t = torch.from_numpy(logits.astype(np.float32))
    opt = torch.optim.LBFGS([t], lr=0.1, max_iter=50)

    def closure():
        opt.zero_grad()
        loss = F.cross_entropy(logits_t / t.clamp(min=1e-2), y_t)
        loss.backward()
        return loss

    opt.step(closure)
    return float(t.detach().clamp(min=0.05, max=10.0).item())


@torch.no_grad()
def _collect_logits(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[np.ndarray, np.ndarray]:
    model.eval()
    logits_all, y_all = [], []
    for xb, yb in loader:
        logits_all.append(model(xb.to(device)).cpu().numpy())
        y_all.append(yb.numpy())
    return np.concatenate(logits_all), np.concatenate(y_all)


def threshold_metrics(
    y: np.ndarray,
    proba: np.ndarray,
    realized: np.ndarray,
    *,
    thr: float,
    stake: float = 15.0,
) -> dict[str, Any]:
    mask = proba >= thr
    n = int(mask.sum())
    coverage = float(mask.mean()) if len(y) else 0.0
    if n == 0:
        return {
            "thr": thr,
            "n": 0,
            "coverage": round(coverage, 4),
            "precision": None,
            "pnl_usdt": 0.0,
            "avg_ret": None,
            "expectancy": None,
        }
    precision = float(y[mask].mean())
    pnl = float((realized[mask] * stake).sum())
    avg_ret = float(realized[mask].mean())
    return {
        "thr": thr,
        "n": n,
        "coverage": round(coverage, 4),
        "precision": round(precision, 4),
        "pnl_usdt": round(pnl, 2),
        "avg_ret": round(avg_ret, 5),
        "expectancy": round(avg_ret, 5),
    }


def pick_threshold(
    y: np.ndarray,
    proba: np.ndarray,
    realized: np.ndarray,
    *,
    gates: tuple[float, ...] = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85),
    min_n: int = 80,
) -> tuple[float, list[dict[str, Any]]]:
    """Pick thr by best positive expectancy with enough trades; else best pnl."""
    curve = [threshold_metrics(y, proba, realized, thr=g) for g in gates]
    ok = [
        c
        for c in curve
        if c["n"] >= min_n and c["avg_ret"] is not None and c["avg_ret"] > 0 and (c["precision"] or 0) >= 0.45
    ]
    if ok:
        best = max(ok, key=lambda c: (c["pnl_usdt"], c["precision"] or 0))
        return float(best["thr"]), curve
    nonempty = [c for c in curve if c["n"] >= max(30, min_n // 2) and c["precision"] is not None]
    if nonempty:
        best = max(nonempty, key=lambda c: (c["pnl_usdt"], c["precision"] or 0))
        return float(best["thr"]), curve
    return 0.65, curve


def train_barrier_transformer(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_va: np.ndarray,
    y_va: np.ndarray,
    realized_va: np.ndarray,
    *,
    epochs: int = 12,
    batch: int = 256,
    lr: float = 8e-4,
    d_model: int = 192,
    nhead: int = 8,
    layers: int = 4,
    dim_ff: int | None = None,
    seed: int = 42,
    device: torch.device | None = None,
) -> BarrierTrainResult:
    device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(seed)
    np.random.seed(seed)
    if device.type == "cuda":
        torch.backends.cudnn.benchmark = True
    print(f"train device={device}", flush=True)

    mean = X_tr.reshape(-1, X_tr.shape[-1]).mean(axis=0).astype(np.float32)
    std = X_tr.reshape(-1, X_tr.shape[-1]).std(axis=0).astype(np.float32)
    std = np.where(std < 1e-6, 1.0, std)

    X_tr_n = _normalize(X_tr, mean, std)
    X_va_n = _normalize(X_va, mean, std)

    pos = float((y_tr == 1).sum())
    neg = float((y_tr == 0).sum())
    w = torch.tensor([1.0, max(neg / max(pos, 1.0), 0.75)], dtype=torch.float32, device=device)

    model = TinyBarrierTransformer(
        n_feat=X_tr.shape[-1],
        d_model=d_model,
        nhead=nhead,
        layers=layers,
        dim_ff=dim_ff,
        max_len=max(128, int(X_tr.shape[1]) + 8),
    ).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"model params={n_params:,} d_model={d_model} layers={layers} nhead={nhead}", flush=True)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(epochs, 1))
    loss_fn = FocalLoss(weight=w, gamma=1.5)

    tr_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_tr_n), torch.from_numpy(y_tr.astype(np.int64))),
        batch_size=batch,
        shuffle=True,
    )
    va_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_va_n), torch.from_numpy(y_va.astype(np.int64))),
        batch_size=batch,
        shuffle=False,
    )

    best_state = None
    best_score = -1e18
    # Score a val subsample each epoch (full val after training)
    va_idx = np.linspace(0, len(y_va) - 1, num=min(4000, len(y_va)), dtype=int)
    X_va_s, y_va_s, r_va_s = X_va[va_idx], y_va[va_idx], realized_va[va_idx]

    for ep in range(epochs):
        model.train()
        total, n = 0.0, 0
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += float(loss.item()) * len(yb)
            n += len(yb)
        sched.step()

        proba_va = predict_proba(model, X_va_s, mean, std, device=device, temperature=1.0, batch=512)
        m = threshold_metrics(y_va_s, proba_va, r_va_s, thr=0.55)
        score = float(m["pnl_usdt"]) + 50.0 * float(m["precision"] or 0)
        if score > best_score:
            best_score = score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(
            f"epoch {ep + 1}/{epochs} train_loss={total / max(n, 1):.4f} "
            f"val@0.55 prec={m['precision']} pnl={m['pnl_usdt']} n={m['n']}",
            flush=True,
        )

    if best_state:
        model.load_state_dict(best_state)

    logits_va, y_va_chk = _collect_logits(model, va_loader, device)
    temperature = fit_temperature(logits_va, y_va_chk)
    proba_va = predict_proba(model, X_va, mean, std, device=device, temperature=temperature)
    thr, curve = pick_threshold(y_va, proba_va, realized_va)

    try:
        from sklearn.metrics import roc_auc_score

        auc = float(roc_auc_score(y_va, proba_va)) if len(np.unique(y_va)) > 1 else None
    except Exception:
        auc = None

    metrics = {
        "val_auc": None if auc is None else round(auc, 4),
        "temperature": round(temperature, 4),
        "chosen_thr": thr,
        "val_curve": curve,
        "n_train": int(len(y_tr)),
        "n_val": int(len(y_va)),
        "pos_rate_train": round(float(y_tr.mean()), 4),
        "pos_rate_val": round(float(y_va.mean()), 4),
        "best_val_score": round(best_score, 2),
    }
    return BarrierTrainResult(
        model=model,
        mean=mean,
        std=std,
        temperature=temperature,
        metrics=metrics,
        device=str(device),
    )


def save_checkpoint(
    path: Path | str,
    result: BarrierTrainResult,
    *,
    meta: dict[str, Any],
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    d_model = int(meta.get("d_model", getattr(result.model, "d_model", 192)))
    nhead = int(meta.get("nhead", getattr(result.model, "nhead", 8)))
    layers = int(meta.get("layers", getattr(result.model, "layers", 4)))
    dim_ff = int(meta.get("dim_ff", getattr(result.model, "dim_ff", 4 * d_model)))
    blob = {
        "state_dict": result.model.state_dict(),
        "mean": result.mean,
        "std": result.std,
        "temperature": result.temperature,
        "n_feat": int(result.mean.shape[0]),
        "feature_cols": list(meta.get("feature_cols") or FEATURE_COLS),
        "d_model": d_model,
        "nhead": nhead,
        "layers": layers,
        "dim_ff": dim_ff,
        "n_params": int(sum(p.numel() for p in result.model.parameters())),
        "version": 4,
        **meta,
    }
    torch.save(blob, path)
    meta_path = path.with_suffix(".meta.json")
    safe = {k: v for k, v in blob.items() if k not in ("state_dict", "mean", "std")}
    safe["mean"] = result.mean.tolist()
    safe["std"] = result.std.tolist()
    safe["metrics"] = result.metrics
    meta_path.write_text(json.dumps(safe, indent=2), encoding="utf-8")


def load_checkpoint(path: Path | str, device: str | None = None) -> dict[str, Any]:
    path = Path(path)
    blob = torch.load(path, map_location="cpu", weights_only=False)
    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    n_feat = int(blob.get("n_feat") or len(blob["mean"]))
    model = TinyBarrierTransformer(
        n_feat=n_feat,
        d_model=int(blob.get("d_model", 192)),
        nhead=int(blob.get("nhead", 8)),
        layers=int(blob.get("layers", 4)),
        dim_ff=blob.get("dim_ff"),
        max_len=max(192, int(blob.get("window", 96)) + 32),
    )
    model.load_state_dict(blob["state_dict"])
    model.to(dev)
    model.eval()
    # exit levels: prefer explicit tp_pct/sl_pct else ATR mults as approx pct
    tp = float(blob.get("tp_pct", blob.get("tp", 0.01)))
    sl = float(blob.get("sl_pct", blob.get("sl", 0.01)))
    return {
        "model": model,
        "mean": np.asarray(blob["mean"], dtype=np.float32),
        "std": np.asarray(blob["std"], dtype=np.float32),
        "temperature": float(blob.get("temperature", 1.0)),
        "window": int(blob.get("window", DEFAULT_WINDOW)),
        "tp": tp,
        "sl": sl,
        "tp_mult": float(blob.get("tp_mult", DEFAULT_TP_MULT)),
        "sl_mult": float(blob.get("sl_mult", DEFAULT_SL_MULT)),
        "horizon": int(blob.get("horizon", DEFAULT_HORIZON)),
        "thr": float(blob.get("thr", blob.get("chosen_thr", 0.65))),
        "feature_cols": list(blob.get("feature_cols") or FEATURE_COLS),
        "vol_z_min": float(blob.get("vol_z_min", DEFAULT_VOL_Z_MIN)),
        "atr_pct_min": float(blob.get("atr_pct_min", DEFAULT_ATR_PCT_MIN)),
        "require_setup": bool(blob.get("require_setup", True)),
        "device": dev,
        "meta": blob,
    }


class BarrierTransformerInferencer:
    """Score long/short setup bars."""

    def __init__(self, model_path: str | Path, *, conf_thr: float | None = None, device: str | None = None):
        pack = load_checkpoint(model_path, device=device)
        self.model = pack["model"]
        self.mean = pack["mean"]
        self.std = pack["std"]
        self.temperature = pack["temperature"]
        self.window = pack["window"]
        self.tp = pack["tp"]
        self.sl = pack["sl"]
        self.horizon = pack["horizon"]
        self.conf_thr = float(conf_thr if conf_thr is not None else pack["thr"])
        self.feature_cols = pack["feature_cols"]
        self.vol_z_min = pack["vol_z_min"]
        self.atr_pct_min = pack["atr_pct_min"]
        meta = pack.get("meta") or {}
        mode = str(meta.get("mode") or "")
        self.require_setup = bool(pack["require_setup"]) and mode != "strategy_gate"
        self.device = pack["device"]
        self._btc: pd.DataFrame | None = None

    def set_btc(self, btc_ohlcv: pd.DataFrame | None) -> None:
        self._btc = btc_ohlcv

    def score_sides(
        self,
        ohlcv: pd.DataFrame,
        *,
        stride: int = 6,
    ) -> tuple[pd.Series, pd.Series]:
        feat = build_feature_frame(ohlcv, self._btc)
        long_p = pd.Series(np.nan, index=ohlcv.index, dtype=float)
        short_p = pd.Series(np.nan, index=ohlcv.index, dtype=float)
        if feat.empty:
            return long_p, short_p
        base = [c for c in self.feature_cols if c != "side"]
        clean = feat.dropna(subset=[c for c in base if c in feat.columns])
        for side, series in ((1, long_p), (-1, short_p)):
            xs, positions = windows_all(
                feat,
                window=self.window,
                side=side,
                feature_cols=self.feature_cols,
                require_setup=self.require_setup,
                vol_z_min=self.vol_z_min,
                atr_pct_min=self.atr_pct_min,
                stride=stride,
            )
            if len(xs) == 0:
                continue
            p = predict_proba(
                self.model,
                xs,
                self.mean,
                self.std,
                device=self.device,
                temperature=self.temperature,
            )
            for pos, pv in zip(positions, p):
                if int(pos) >= len(clean.index):
                    continue
                ts = clean.index[int(pos)]
                try:
                    series.loc[ts] = float(pv)
                except Exception:
                    pass
        return long_p.reindex(ohlcv.index), short_p.reindex(ohlcv.index)

    def score_frame(self, ohlcv: pd.DataFrame, *, stride: int = 1) -> pd.Series:
        """Back-compat: long-only proba."""
        long_p, _ = self.score_sides(ohlcv, stride=stride)
        return long_p

    def decide_last(self, ohlcv: pd.DataFrame) -> dict[str, Any]:
        feat = build_feature_frame(ohlcv, self._btc)
        if feat.empty or len(feat) < self.window:
            return {"take": False, "side": None, "p_tp_first": None, "reason": "short_history"}
        last = feat.iloc[-1]
        if self.require_setup and not is_setup_row(last, vol_z_min=self.vol_z_min, atr_pct_min=self.atr_pct_min):
            return {"take": False, "side": None, "p_tp_first": None, "reason": "no_setup"}
        best_side, best_p = None, -1.0
        for side, name in ((1, "long"), (-1, "short")):
            x = last_window(feat, window=self.window, side=side, feature_cols=self.feature_cols)
            if x is None:
                continue
            p = float(
                predict_proba(
                    self.model,
                    x,
                    self.mean,
                    self.std,
                    device=self.device,
                    temperature=self.temperature,
                )[0]
            )
            if p > best_p:
                best_p, best_side = p, name
        take = best_p >= self.conf_thr and best_side is not None
        return {
            "take": take,
            "side": best_side if take else None,
            "p_tp_first": round(best_p, 4) if best_p >= 0 else None,
            "thr": self.conf_thr,
            "tp": self.tp,
            "sl": self.sl,
            "reason": "ok" if take else "low_conf",
        }
