#!/usr/bin/env python3
"""Compare altcoin pairs vs BTC: reaction lag (cross-corr) + GRU follow-strength.

Positive lag (bars) = alt reacts AFTER BTC.
Negative lag = alt leads BTC.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.datastore import HistoricalDatastore  # noqa: E402
from simulation.scripts.comparison_common import pairs_from_source  # noqa: E402

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}


@dataclass
class PairLagResult:
    pair: str
    base: str
    n_bars: int
    corr_0: float
    lag_bars: int
    lag_minutes: int
    corr_at_lag: float
    follower_lag_bars: int
    follower_lag_minutes: int
    follower_corr: float
    beta_at_lag: float
    gru_r2: float
    gru_r2_vs_shuffle: float
    follow_score: float
    cluster: str


class BtcFollowerGRU(nn.Module):
    """Maps a BTC return window -> expected alt return at the end of the window."""

    def __init__(self, hidden: int = 24):
        super().__init__()
        self.gru = nn.GRU(input_size=1, hidden_size=hidden, batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, T, 1]
        out, _ = self.gru(x)
        return self.head(out[:, -1, :]).squeeze(-1)


def _returns(close: pd.Series) -> pd.Series:
    r = np.log(close.astype(float)).diff()
    return r.replace([np.inf, -np.inf], np.nan)


def _best_lag(
    btc: np.ndarray, alt: np.ndarray, max_lag: int
) -> tuple[int, float, float, int, float]:
    """Return (best_lag, corr_at_best, beta, follower_lag>=0, follower_corr).

    Positive lag => alt reacts AFTER BTC.
    """
    corr0 = float(np.corrcoef(btc, alt)[0, 1]) if len(btc) > 20 else float("nan")
    best_lag = 0
    best_corr = corr0
    follow_lag = 0
    follow_corr = corr0
    for lag in range(-max_lag, max_lag + 1):
        if lag == 0:
            c = corr0
        elif lag > 0:
            x, y = btc[:-lag], alt[lag:]
            c = float(np.corrcoef(x, y)[0, 1]) if len(x) > 20 else float("nan")
        else:
            L = -lag
            x, y = btc[L:], alt[:-L]
            c = float(np.corrcoef(x, y)[0, 1]) if len(x) > 20 else float("nan")
        if not math.isfinite(c):
            continue
        if abs(c) > abs(best_corr):
            best_corr = c
            best_lag = lag
        if lag >= 0 and abs(c) > abs(follow_corr):
            follow_corr = c
            follow_lag = lag

    if best_lag > 0:
        x, y = btc[:-best_lag], alt[best_lag:]
    elif best_lag < 0:
        L = -best_lag
        x, y = btc[L:], alt[:-L]
    else:
        x, y = btc, alt
    var = float(np.var(x) + 1e-12)
    beta = float(np.cov(x, y, ddof=0)[0, 1] / var)
    return best_lag, float(best_corr), beta, follow_lag, float(follow_corr)


def _make_windows(
    btc: np.ndarray, alt: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for i in range(window, len(btc)):
        xs.append(btc[i - window : i])
        ys.append(alt[i - 1])  # predict alt return contemporaneous with last BTC bar
    x = np.asarray(xs, dtype=np.float32)[..., None]
    y = np.asarray(ys, dtype=np.float32)
    return x, y


def _r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = float(((y_true - y_pred) ** 2).sum())
    ss_tot = float(((y_true - y_true.mean()) ** 2).sum() + 1e-12)
    return 1.0 - ss_res / ss_tot


@torch.no_grad()
def _eval_r2(model: nn.Module, loader: DataLoader, device: torch.device) -> float:
    model.eval()
    yt, yp = [], []
    for xb, yb in loader:
        xb = xb.to(device)
        pred = model(xb).cpu().numpy()
        yt.append(yb.numpy())
        yp.append(pred)
    return _r2(np.concatenate(yt), np.concatenate(yp))


def train_gru_follow(
    btc: np.ndarray,
    alt: np.ndarray,
    *,
    window: int,
    epochs: int,
    batch: int,
    device: torch.device,
    seed: int = 42,
) -> tuple[float, float]:
    """Fit GRU on BTC->alt; return (r2_test, r2_test - r2_shuffle)."""
    x, y = _make_windows(btc, alt, window)
    if len(y) < 200:
        return float("nan"), float("nan")

    n = len(y)
    split = int(n * 0.8)
    if n - split < 50:
        return float("nan"), float("nan")

    def _fit(x_all: np.ndarray, y_all: np.ndarray) -> float:
        torch.manual_seed(seed)
        model = BtcFollowerGRU(hidden=24).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)
        x_tr_t = torch.from_numpy(x_all[:split])
        y_tr_t = torch.from_numpy(y_all[:split])
        x_te_t = torch.from_numpy(x_all[split:])
        y_te_t = torch.from_numpy(y_all[split:])
        tr = DataLoader(TensorDataset(x_tr_t, y_tr_t), batch_size=batch, shuffle=True)
        te = DataLoader(TensorDataset(x_te_t, y_te_t), batch_size=batch, shuffle=False)
        model.train()
        for _ in range(epochs):
            for xb, yb in tr:
                xb, yb = xb.to(device), yb.to(device)
                opt.zero_grad()
                loss = nn.functional.mse_loss(model(xb), yb)
                loss.backward()
                opt.step()
        return _eval_r2(model, te, device)

    r2 = _fit(x, y)
    rng = np.random.default_rng(seed)
    btc_shuf = btc.copy()
    rng.shuffle(btc_shuf)
    x_s, y_s = _make_windows(btc_shuf, alt, window)
    r2_s = _fit(x_s, y_s)
    return float(r2), float(r2 - r2_s)


def _cluster(lag_bars: int, corr: float, follow: float, follower_lag: int) -> str:
    if not math.isfinite(corr) or abs(corr) < 0.12:
        return "weak_link"
    # Prefer follower_lag for labeling reaction delay
    use_lag = follower_lag if follower_lag > 0 else lag_bars
    if use_lag <= 0 and abs(corr) >= 0.35:
        return "sync_or_lead"
    if 1 <= use_lag <= 2:
        return "fast_follower"
    if 3 <= use_lag <= 6:
        return "mid_follower"
    if use_lag > 6:
        return "slow_follower"
    if follow > 0.02:
        return "gru_follower"
    return "mixed"


def analyze_pair(
    btc_r: pd.Series,
    alt_close: pd.Series,
    pair: str,
    *,
    max_lag: int,
    window: int,
    epochs: int,
    batch: int,
    device: torch.device,
    bar_minutes: int,
    use_gru: bool = True,
) -> PairLagResult | None:
    alt_r = _returns(alt_close)
    df = pd.concat({"btc": btc_r, "alt": alt_r}, axis=1).dropna()
    if len(df) < max(500, window + 100):
        return None
    b = df["btc"].to_numpy(dtype=np.float64)
    a = df["alt"].to_numpy(dtype=np.float64)
    lag, corr_lag, beta, f_lag, f_corr = _best_lag(b, a, max_lag)
    corr0 = float(np.corrcoef(b, a)[0, 1])
    if use_gru:
        gru_r2, gru_delta = train_gru_follow(
            b, a, window=window, epochs=epochs, batch=batch, device=device
        )
    else:
        gru_r2, gru_delta = float("nan"), 0.0
    follow = float(gru_delta) if math.isfinite(gru_delta) else 0.0
    score = abs(f_corr) * (1.0 + max(0.0, follow) * 10.0)
    return PairLagResult(
        pair=pair,
        base=pair.split("/")[0],
        n_bars=len(df),
        corr_0=round(corr0, 4),
        lag_bars=int(lag),
        lag_minutes=int(lag * bar_minutes),
        corr_at_lag=round(corr_lag, 4),
        follower_lag_bars=int(f_lag),
        follower_lag_minutes=int(f_lag * bar_minutes),
        follower_corr=round(f_corr, 4),
        beta_at_lag=round(beta, 4),
        gru_r2=round(gru_r2, 4) if math.isfinite(gru_r2) else float("nan"),
        gru_r2_vs_shuffle=round(gru_delta, 4) if math.isfinite(gru_delta) else float("nan"),
        follow_score=round(score, 4),
        cluster=_cluster(lag, corr_lag, follow, f_lag),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="prod200", help="prod200|pool|all|grid_priority")
    ap.add_argument("--limit", type=int, default=0, help="max alts (0=all)")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--timerange", default="20260601-20260716", help="YYYYMMDD-YYYYMMDD")
    ap.add_argument("--max-lag", type=int, default=12, help="bars to search (+/-)")
    ap.add_argument("--window", type=int, default=24, help="GRU input bars")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--no-gru", action="store_true", help="skip GRU (corr/lag only, faster)")
    ap.add_argument(
        "--out",
        default=str(ROOT / "simulation" / "data" / "btc_pair_lag_gru.json"),
    )
    args = ap.parse_args()

    datadir = ROOT / "simulation" / "data" / "freqtrade"
    ds = HistoricalDatastore(datadir)
    bar_min = TF_MINUTES.get(args.timeframe, 5)

    start_s, end_s = args.timerange.split("-")
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC")

    btc = ds.load("BTC/USDT:USDT", args.timeframe)
    btc = btc.loc[(btc.index >= start) & (btc.index < end)]
    btc_r = _returns(btc["close"]).dropna()
    if len(btc_r) < 500:
        raise SystemExit(f"BTC series too short in {args.timerange}: {len(btc_r)}")

    pairs = [p for p in pairs_from_source(ROOT, args.pairs) if not p.startswith("BTC/")]
    if args.limit and args.limit > 0:
        pairs = pairs[: args.limit]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device} pairs={len(pairs)} tf={args.timeframe} range={args.timerange}")

    results: list[PairLagResult] = []
    for i, pair in enumerate(pairs, 1):
        try:
            df = ds.load(pair, args.timeframe)
        except FileNotFoundError:
            print(f"[{i}/{len(pairs)}] SKIP no data {pair}")
            continue
        df = df.loc[(df.index >= start) & (df.index < end)]
        if df.empty or "close" not in df.columns:
            print(f"[{i}/{len(pairs)}] SKIP empty {pair}")
            continue
        rec = analyze_pair(
            btc_r,
            df["close"],
            pair,
            max_lag=args.max_lag,
            window=args.window,
            epochs=args.epochs,
            batch=args.batch,
            device=device,
            bar_minutes=bar_min,
            use_gru=not args.no_gru,
        )
        if rec is None:
            print(f"[{i}/{len(pairs)}] SKIP short {pair}")
            continue
        results.append(rec)
        print(
            f"[{i}/{len(pairs)}] {rec.base:12} lag={rec.lag_minutes:+4d}m "
            f"follow={rec.follower_lag_minutes:3d}m corr0={rec.corr_0:+.3f} "
            f"beta={rec.beta_at_lag:+.2f} gru_d={rec.gru_r2_vs_shuffle:+.3f} "
            f"{rec.cluster}"
        )

    results.sort(key=lambda r: (r.lag_minutes, -abs(r.corr_at_lag)))
    out = {
        "meta": {
            "benchmark": "BTC/USDT:USDT",
            "timeframe": args.timeframe,
            "timerange": args.timerange,
            "max_lag_bars": args.max_lag,
            "gru_window_bars": args.window,
            "gru_epochs": args.epochs,
            "device": str(device),
            "n_pairs": len(results),
            "note": (
                "lag_minutes>0: alt reacts AFTER BTC; "
                "gru_r2_vs_shuffle: how much BTC sequence beats shuffled noise"
            ),
        },
        "summary": _summary(results),
        "pairs": [asdict(r) for r in results],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path} ({len(results)} pairs)")
    _print_summary(out["summary"])
    return 0


def _summary(rows: list[PairLagResult]) -> dict:
    if not rows:
        return {}
    lags = [r.follower_lag_minutes for r in rows]
    by_c: dict[str, int] = {}
    for r in rows:
        by_c[r.cluster] = by_c.get(r.cluster, 0) + 1
    fastest = sorted(rows, key=lambda r: (r.follower_lag_minutes, -abs(r.follower_corr)))[:20]
    slowest = sorted(rows, key=lambda r: (-r.follower_lag_minutes, -abs(r.follower_corr)))[:20]
    strongest = sorted(rows, key=lambda r: -r.follow_score)[:20]
    weakest = sorted(rows, key=lambda r: abs(r.corr_0))[:15]
    return {
        "median_follower_lag_min": float(np.median(lags)),
        "mean_follower_lag_min": float(np.mean(lags)),
        "pct_lag0_sync": round(100 * sum(1 for r in rows if r.follower_lag_minutes == 0) / len(rows), 1),
        "pct_delayed": round(100 * sum(1 for r in rows if r.follower_lag_minutes > 0) / len(rows), 1),
        "clusters": by_c,
        "fastest": [asdict(r) for r in fastest],
        "slowest": [asdict(r) for r in slowest],
        "strongest_followers": [asdict(r) for r in strongest],
        "weakest_link": [asdict(r) for r in weakest],
    }


def _print_summary(s: dict) -> None:
    if not s:
        return
    print("\n=== SUMMARY ===")
    print(
        f"median follower lag: {s['median_follower_lag_min']:.0f} min | "
        f"mean: {s['mean_follower_lag_min']:.1f} min"
    )
    print(f"sync (0 lag): {s['pct_lag0_sync']}% | delayed: {s['pct_delayed']}%")
    print("clusters:", s["clusters"])
    print("\nFastest (sync with BTC):")
    for r in s["fastest"][:10]:
        print(
            f"  {r['base']:12} follow={r['follower_lag_minutes']:3d}m "
            f"corr={r['follower_corr']:+.3f} beta={r['beta_at_lag']:+.2f}"
        )
    print("\nSlowest followers:")
    for r in s["slowest"][:10]:
        print(
            f"  {r['base']:12} follow={r['follower_lag_minutes']:3d}m "
            f"corr={r['follower_corr']:+.3f} beta={r['beta_at_lag']:+.2f}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
