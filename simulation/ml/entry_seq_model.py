"""Small LSTM / Transformer encoders for trade-entry profit classification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class SeqTrainResult:
    model: Any
    mean: np.ndarray
    std: np.ndarray
    proba_test: np.ndarray
    metrics: dict[str, Any]


def _normalize(x: np.ndarray, mean: np.ndarray | None = None, std: np.ndarray | None = None):
    # x: [N,T,F]
    if mean is None:
        mean = np.nanmean(x.reshape(-1, x.shape[-1]), axis=0)
        std = np.nanstd(x.reshape(-1, x.shape[-1]), axis=0)
        std = np.where(std < 1e-6, 1.0, std)
    xn = (x - mean) / std
    return np.nan_to_num(xn, nan=0.0).astype(np.float32), mean.astype(np.float32), std.astype(np.float32)


def train_seq_classifier(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_te: np.ndarray,
    y_te: np.ndarray,
    *,
    kind: str = "lstm",
    epochs: int = 8,
    batch: int = 256,
    lr: float = 1e-3,
    seed: int = 42,
) -> SeqTrainResult:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cpu")

    X_tr_n, mean, std = _normalize(X_tr)
    X_te_n, _, _ = _normalize(X_te, mean, std)

    n_f = X_tr.shape[-1]
    hidden = 32

    class LSTMCls(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(n_f, hidden, batch_first=True, num_layers=1)
            self.head = nn.Sequential(nn.Linear(hidden, 16), nn.ReLU(), nn.Linear(16, 2))

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.head(out[:, -1, :])

    class TinyTransformerCls(nn.Module):
        def __init__(self):
            super().__init__()
            self.in_proj = nn.Linear(n_f, hidden)
            enc_layer = nn.TransformerEncoderLayer(
                d_model=hidden,
                nhead=4,
                dim_feedforward=64,
                batch_first=True,
                dropout=0.1,
            )
            self.enc = nn.TransformerEncoder(enc_layer, num_layers=1)
            self.head = nn.Sequential(nn.Linear(hidden, 16), nn.ReLU(), nn.Linear(16, 2))

        def forward(self, x):
            h = self.in_proj(x)
            h = self.enc(h)
            # attention-ish: mean pool + last
            pooled = 0.5 * (h.mean(dim=1) + h[:, -1, :])
            return self.head(pooled)

    model: nn.Module = TinyTransformerCls() if kind == "transformer" else LSTMCls()
    model.to(device)

    # class balance weights
    pos = float((y_tr == 1).sum())
    neg = float((y_tr == 0).sum())
    w = torch.tensor([1.0, max(neg / max(pos, 1.0), 0.5)], dtype=torch.float32, device=device)
    loss_fn = nn.CrossEntropyLoss(weight=w)
    opt = torch.optim.Adam(model.parameters(), lr=lr)

    y_tr_t = torch.from_numpy(y_tr.astype(np.int64))
    y_te_t = torch.from_numpy(y_te.astype(np.int64))
    tr_loader = DataLoader(
        TensorDataset(torch.from_numpy(X_tr_n), y_tr_t),
        batch_size=batch,
        shuffle=True,
    )
    te_x = torch.from_numpy(X_te_n)

    best_state = None
    best_loss = 1e9
    model.train()
    for _ in range(epochs):
        total = 0.0
        n = 0
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            logits = model(xb)
            loss = loss_fn(logits, yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += float(loss.item()) * len(yb)
            n += len(yb)
        avg = total / max(n, 1)
        if avg < best_loss:
            best_loss = avg
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state:
        model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        logits = model(te_x.to(device))
        proba = torch.softmax(logits, dim=1).cpu().numpy()[:, 1]

    # metrics
    pred = (proba >= 0.5).astype(int)
    acc = float((pred == y_te).mean()) if len(y_te) else 0.0
    try:
        from sklearn.metrics import roc_auc_score

        auc = float(roc_auc_score(y_te, proba)) if len(np.unique(y_te)) > 1 else None
    except Exception:
        auc = None

    return SeqTrainResult(
        model=model,
        mean=mean,
        std=std,
        proba_test=proba.astype(float),
        metrics={"accuracy": round(acc, 4), "roc_auc": None if auc is None else round(auc, 4)},
    )
