"""Sequence gate classifiers for crypto entry meta-labeling.

Architectures (all binary: take / skip on strategy entry windows):
- tiny_transformer: pointwise encoder (current baseline)
- patch_tst: patch tokens + Transformer
- itransformer: inverted attention over features
- gru / bilstm_attn: recurrent
- modern_tcn: large-kernel temporal conv
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from simulation.ml.barrier_transformer import (
    FocalLoss,
    fit_temperature,
    pick_threshold,
    predict_proba,
    threshold_metrics,
)


def _normalize(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return ((x - mean) / (std + 1e-8)).astype(np.float32)


@dataclass
class SeqGateResult:
    name: str
    model: nn.Module
    mean: np.ndarray
    std: np.ndarray
    temperature: float
    thr: float
    n_params: int
    device: str
    val_metrics: dict[str, Any]
    test_metrics: dict[str, Any]
    proba_test: np.ndarray


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TinyTransformerGate(nn.Module):
    def __init__(self, n_feat: int, d_model: int = 64, nhead: int = 4, layers: int = 2, dropout: float = 0.1):
        super().__init__()
        self.in_proj = nn.Sequential(nn.Linear(n_feat, d_model), nn.LayerNorm(d_model))
        pe = torch.zeros(128, d_model)
        pos = torch.arange(0, 128, dtype=torch.float).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0), persistent=False)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=4 * d_model, batch_first=True, dropout=dropout, activation="gelu"
        )
        self.enc = nn.TransformerEncoder(layer, num_layers=layers)
        self.head = nn.Sequential(nn.Linear(d_model, d_model // 2), nn.GELU(), nn.Dropout(dropout), nn.Linear(d_model // 2, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.in_proj(x) + self.pe[:, : x.size(1), :]
        h = self.enc(h)
        return self.head(0.5 * (h.mean(1) + h[:, -1, :]))


class PatchTSTGate(nn.Module):
    """Patch time-series Transformer → binary logits."""

    def __init__(
        self,
        n_feat: int,
        *,
        d_model: int = 64,
        nhead: int = 4,
        layers: int = 2,
        patch_len: int = 8,
        stride: int = 8,
        dropout: float = 0.1,
    ):
        super().__init__()
        self.patch_len = patch_len
        self.stride = stride
        self.proj = nn.Linear(patch_len * n_feat, d_model)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=4 * d_model, batch_first=True, dropout=dropout, activation="gelu"
        )
        self.enc = nn.TransformerEncoder(layer, num_layers=layers)
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Sequential(nn.Linear(d_model, d_model // 2), nn.GELU(), nn.Dropout(dropout), nn.Linear(d_model // 2, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B,T,F]
        b, t, f = x.shape
        patches = []
        for i in range(0, max(1, t - self.patch_len + 1), self.stride):
            patches.append(x[:, i : i + self.patch_len, :].reshape(b, -1))
        if not patches:
            patches = [x.reshape(b, -1)[:, : self.patch_len * f]]
        p = torch.stack(patches, dim=1)  # [B,Np,P*F]
        if p.size(-1) != self.proj.in_features:
            # pad/truncate feature dim
            need = self.proj.in_features
            if p.size(-1) < need:
                p = F.pad(p, (0, need - p.size(-1)))
            else:
                p = p[..., :need]
        h = self.proj(p)
        h = self.norm(self.enc(h))
        return self.head(h.mean(1))


class ITransformerGate(nn.Module):
    """Inverted Transformer: tokens = features, each sees full time series."""

    def __init__(self, n_feat: int, seq_len: int, d_model: int = 64, nhead: int = 4, layers: int = 2, dropout: float = 0.1):
        super().__init__()
        self.n_feat = n_feat
        self.seq_len = seq_len
        self.proj = nn.Linear(seq_len, d_model)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=4 * d_model, batch_first=True, dropout=dropout, activation="gelu"
        )
        self.enc = nn.TransformerEncoder(layer, num_layers=layers)
        self.head = nn.Sequential(nn.Linear(d_model, d_model // 2), nn.GELU(), nn.Dropout(dropout), nn.Linear(d_model // 2, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x [B,T,F] -> [B,F,T]
        b, t, f = x.shape
        if t != self.seq_len:
            if t < self.seq_len:
                x = F.pad(x, (0, 0, 0, self.seq_len - t))
            else:
                x = x[:, -self.seq_len :, :]
        v = x.transpose(1, 2)  # [B,F,T]
        h = self.proj(v)
        h = self.enc(h)
        return self.head(h.mean(1))


class GRUGate(nn.Module):
    def __init__(self, n_feat: int, hidden: int = 64, layers: int = 2, dropout: float = 0.15):
        super().__init__()
        self.gru = nn.GRU(n_feat, hidden, num_layers=layers, batch_first=True, dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Dropout(dropout), nn.Linear(hidden // 2, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.gru(x)
        return self.head(out[:, -1, :])


class BiLSTMAttnGate(nn.Module):
    def __init__(self, n_feat: int, hidden: int = 64, layers: int = 1, dropout: float = 0.15):
        super().__init__()
        self.lstm = nn.LSTM(
            n_feat, hidden, num_layers=layers, batch_first=True, bidirectional=True, dropout=dropout if layers > 1 else 0.0
        )
        self.attn = nn.Linear(hidden * 2, 1)
        self.head = nn.Sequential(
            nn.Linear(hidden * 2, hidden), nn.ReLU(), nn.Dropout(dropout), nn.Linear(hidden, 2)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, _ = self.lstm(x)  # [B,T,2H]
        w = torch.softmax(self.attn(h).squeeze(-1), dim=1)  # [B,T]
        ctx = torch.sum(h * w.unsqueeze(-1), dim=1)
        return self.head(ctx)


class ModernTCNGate(nn.Module):
    """Large-kernel residual TCN stack."""

    def __init__(self, n_feat: int, channels: int = 64, layers: int = 4, kernel: int = 7, dropout: float = 0.1):
        super().__init__()
        self.inp = nn.Conv1d(n_feat, channels, 1)
        blocks = []
        for i in range(layers):
            dil = 2**i
            pad = dil * (kernel - 1) // 2
            blocks.append(
                nn.Sequential(
                    nn.Conv1d(channels, channels, kernel, padding=pad, dilation=dil),
                    nn.GELU(),
                    nn.Dropout(dropout),
                    nn.Conv1d(channels, channels, 1),
                    nn.GELU(),
                )
            )
        self.blocks = nn.ModuleList(blocks)
        self.head = nn.Sequential(
            nn.Linear(channels, channels // 2), nn.GELU(), nn.Dropout(dropout), nn.Linear(channels // 2, 2)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.inp(x.transpose(1, 2))
        for blk in self.blocks:
            y = blk(h)
            m = min(h.size(-1), y.size(-1))
            h = h[..., :m] + y[..., :m]
        return self.head(h.mean(-1))


MODEL_BUILDERS: dict[str, Callable[..., nn.Module]] = {}


def register_builders(seq_len: int, n_feat: int) -> dict[str, Callable[[], nn.Module]]:
    return {
        "tiny_transformer": lambda: TinyTransformerGate(n_feat, d_model=64, nhead=4, layers=2),
        "patch_tst": lambda: PatchTSTGate(n_feat, d_model=64, nhead=4, layers=2, patch_len=8, stride=8),
        "itransformer": lambda: ITransformerGate(n_feat, seq_len=seq_len, d_model=64, nhead=4, layers=2),
        "gru": lambda: GRUGate(n_feat, hidden=64, layers=2),
        "bilstm_attn": lambda: BiLSTMAttnGate(n_feat, hidden=64, layers=1),
        "modern_tcn": lambda: ModernTCNGate(n_feat, channels=64, layers=4, kernel=7),
    }


# ---------------------------------------------------------------------------
# Train / eval
# ---------------------------------------------------------------------------


def train_one(
    name: str,
    model: nn.Module,
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_va: np.ndarray,
    y_va: np.ndarray,
    realized_va: np.ndarray,
    X_te: np.ndarray,
    y_te: np.ndarray,
    realized_te: np.ndarray,
    *,
    epochs: int = 8,
    batch: int = 128,
    lr: float = 1e-3,
    seed: int = 42,
    device: torch.device | None = None,
) -> SeqGateResult:
    device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(seed)
    np.random.seed(seed)

    mean = X_tr.reshape(-1, X_tr.shape[-1]).mean(0).astype(np.float32)
    std = X_tr.reshape(-1, X_tr.shape[-1]).std(0).astype(np.float32)
    std = np.where(std < 1e-6, 1.0, std)
    X_tr_n, X_va_n, X_te_n = _normalize(X_tr, mean, std), _normalize(X_va, mean, std), _normalize(X_te, mean, std)

    pos = float((y_tr == 1).sum())
    neg = float((y_tr == 0).sum())
    w = torch.tensor([1.0, max(neg / max(pos, 1.0), 0.75)], dtype=torch.float32, device=device)
    model = model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    loss_fn = FocalLoss(weight=w, gamma=1.5)
    n_params = sum(p.numel() for p in model.parameters())

    tr_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_tr_n), torch.from_numpy(y_tr.astype(np.int64))),
        batch_size=batch,
        shuffle=True,
    )
    va_idx = np.linspace(0, len(y_va) - 1, num=min(4000, len(y_va)), dtype=int)

    best_state, best_score = None, -1e18
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
        with torch.no_grad():
            proba = predict_proba(model, X_va[va_idx], mean, std, device=device, temperature=1.0)
        m = threshold_metrics(y_va[va_idx], proba, realized_va[va_idx], thr=0.55)
        score = float(m["pnl_usdt"]) + 40.0 * float(m["precision"] or 0)
        if score >= best_score:
            best_score = score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        print(
            f"  [{name}] epoch {ep+1}/{epochs} loss={total/max(n,1):.4f} "
            f"val@0.55 prec={m['precision']} pnl={m['pnl_usdt']} n={m['n']}",
            flush=True,
        )

    if best_state:
        model.load_state_dict(best_state)

    # temperature on full val
    model.eval()
    with torch.no_grad():
        logits = []
        for i in range(0, len(X_va_n), batch):
            logits.append(model(torch.from_numpy(X_va_n[i : i + batch]).to(device)).cpu().numpy())
        logits_va = np.concatenate(logits)
    temperature = fit_temperature(logits_va, y_va)
    proba_va = predict_proba(model, X_va, mean, std, device=device, temperature=temperature)
    thr, _ = pick_threshold(y_va, proba_va, realized_va, min_n=80)
    val_at = threshold_metrics(y_va, proba_va, realized_va, thr=thr)

    proba_te = predict_proba(model, X_te, mean, std, device=device, temperature=temperature)
    test_at = threshold_metrics(y_te, proba_te, realized_te, thr=thr)
    try:
        from sklearn.metrics import roc_auc_score

        val_auc = float(roc_auc_score(y_va, proba_va)) if len(np.unique(y_va)) > 1 else None
        test_auc = float(roc_auc_score(y_te, proba_te)) if len(np.unique(y_te)) > 1 else None
    except Exception:
        val_auc = test_auc = None

    return SeqGateResult(
        name=name,
        model=model,
        mean=mean,
        std=std,
        temperature=float(temperature),
        thr=float(thr),
        n_params=int(n_params),
        device=str(device),
        val_metrics={"auc": None if val_auc is None else round(val_auc, 4), "at_thr": val_at},
        test_metrics={"auc": None if test_auc is None else round(test_auc, 4), "at_thr": test_at},
        proba_test=proba_te,
    )


def save_seq_gate_checkpoint(
    path: Path | str,
    result: SeqGateResult,
    *,
    arch: str,
    window: int,
    feature_cols: list[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    """Persist seq-gate weights + calibration for live inference."""
    from simulation.ml.barrier_transformer import FEATURE_COLS

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    model = result.model.cpu()
    blob: dict[str, Any] = {
        "arch": arch,
        "state_dict": model.state_dict(),
        "mean": result.mean,
        "std": result.std,
        "temperature": float(result.temperature),
        "thr": float(result.thr),
        "window": int(window),
        "n_feat": int(result.mean.shape[0]),
        "feature_cols": list(feature_cols or FEATURE_COLS),
        "n_params": int(result.n_params),
        "require_setup": False,
        "mode": "strategy_gate",
        "tp_pct": 0.008,
        "sl_pct": 0.01,
        "version": 1,
        "metrics": {
            "val": result.val_metrics,
            "test": result.test_metrics,
        },
    }
    if extra_meta:
        blob.update(extra_meta)
    torch.save(blob, path)
    meta_path = path.with_suffix(".meta.json")
    safe = {k: v for k, v in blob.items() if k not in ("state_dict", "mean", "std")}
    safe["mean"] = result.mean.tolist()
    safe["std"] = result.std.tolist()
    meta_path.write_text(
        __import__("json").dumps(safe, indent=2),
        encoding="utf-8",
    )
    return path


def load_seq_gate_checkpoint(path: Path | str, *, device: str | None = None) -> dict[str, Any]:
    path = Path(path)
    blob = torch.load(path, map_location="cpu", weights_only=False)
    arch = str(blob.get("arch") or "gru")
    window = int(blob.get("window", 64))
    n_feat = int(blob.get("n_feat") or len(blob["mean"]))
    builders = register_builders(seq_len=window, n_feat=n_feat)
    if arch not in builders:
        raise ValueError(f"unknown seq-gate arch: {arch}")
    model = builders[arch]()
    model.load_state_dict(blob["state_dict"])
    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    model.to(dev)
    model.eval()
    from simulation.ml.barrier_transformer import FEATURE_COLS

    return {
        "model": model,
        "mean": np.asarray(blob["mean"], dtype=np.float32),
        "std": np.asarray(blob["std"], dtype=np.float32),
        "temperature": float(blob.get("temperature", 1.0)),
        "window": window,
        "thr": float(blob.get("thr", 0.85)),
        "tp": float(blob.get("tp_pct", blob.get("tp", 0.008))),
        "sl": float(blob.get("sl_pct", blob.get("sl", 0.01))),
        "feature_cols": list(blob.get("feature_cols") or FEATURE_COLS),
        "require_setup": bool(blob.get("require_setup", False)),
        "vol_z_min": float(blob.get("vol_z_min", 1.2)),
        "atr_pct_min": float(blob.get("atr_pct_min", 0.002)),
        "device": dev,
        "arch": arch,
        "meta": blob,
    }


class SeqGateInferencer:
    """Score long/short bars with a trained seq-gate model (GRU / …)."""

    def __init__(self, model_path: str | Path, *, conf_thr: float | None = None, device: str | None = None):
        from simulation.ml.barrier_transformer import (
            build_feature_frame,
            predict_proba,
            windows_all,
        )

        self._build_feature_frame = build_feature_frame
        self._predict_proba = predict_proba
        self._windows_all = windows_all
        pack = load_seq_gate_checkpoint(model_path, device=device)
        self.model = pack["model"]
        self.mean = pack["mean"]
        self.std = pack["std"]
        self.temperature = pack["temperature"]
        self.window = pack["window"]
        self.tp = pack["tp"]
        self.sl = pack["sl"]
        self.conf_thr = float(conf_thr if conf_thr is not None else pack["thr"])
        self.feature_cols = pack["feature_cols"]
        self.require_setup = bool(pack["require_setup"])
        self.vol_z_min = pack["vol_z_min"]
        self.atr_pct_min = pack["atr_pct_min"]
        self.device = pack["device"]
        self.arch = pack["arch"]
        self._btc = None

    def set_btc(self, btc_ohlcv) -> None:
        self._btc = btc_ohlcv

    def score_sides(self, ohlcv, *, stride: int = 3):
        import pandas as pd

        feat = self._build_feature_frame(ohlcv, self._btc)
        long_p = pd.Series(np.nan, index=ohlcv.index, dtype=float)
        short_p = pd.Series(np.nan, index=ohlcv.index, dtype=float)
        if feat.empty:
            return long_p, short_p
        base = [c for c in self.feature_cols if c != "side"]
        clean = feat.dropna(subset=[c for c in base if c in feat.columns])
        for side, series in ((1, long_p), (-1, short_p)):
            xs, positions = self._windows_all(
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
            p = self._predict_proba(
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
